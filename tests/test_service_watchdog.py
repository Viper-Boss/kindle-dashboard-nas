import copy
import json
from pathlib import Path
import struct
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch
import maintenance
import proxy_admin as pa
import service_watchdog as sw


class MemoryStore:
    def __init__(self):
        self.data = {}
    def read(self, name, default=None):
        return copy.deepcopy(self.data.get(name, default))
    def write(self, name, value):
        self.data[name] = copy.deepcopy(value)


class Core:
    def __init__(self):
        self.mode = 'rule'
        self.proxies = {n: {'type': 'Hysteria2', 'name': n} for n in ('A', 'B', 'C')}
        for name in sw.GROUPS.values():
            self.proxies[name] = {'type': 'Selector', 'now': 'A', 'all': ['A', 'B', 'C']}
        self.proxies[sw.PROBE_GROUP] = {'type': 'Selector', 'now': 'A', 'all': ['A', 'B', 'C'], 'hidden': True}
        self.writes, self.deleted = [], []

    def __call__(self, path, method='GET', data=None):
        if path == '/configs':
            return {'mode': self.mode}
        if path == '/providers/proxies':
            return {'providers': {}}
        if path == '/proxies':
            return {'proxies': copy.deepcopy(self.proxies)}
        if path == '/connections':
            return {'connections': [
                {'id': 'wa-old', 'chains': ['A', sw.GROUPS['whatsapp']]},
                {'id': 'tg', 'chains': ['A', sw.GROUPS['telegram']]},
                {'id': 'wa-new', 'chains': ['B', sw.GROUPS['whatsapp']]},
                {'id': 'general', 'chains': ['A', '数据屏代理']}]}
        if path.startswith('/connections/'):
            self.deleted.append(path.rsplit('/', 1)[1])
            return {}
        name = urllib.parse.unquote(path.split('/proxies/', 1)[1])
        if method == 'PUT':
            self.proxies[name]['now'] = data['name']
            self.writes.append((name, data['name']))
            return {}
        return copy.deepcopy(self.proxies[name])


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore()
        self.core = Core()
        self.now = 10000
        self.result = {'A': False, 'B': True, 'C': False}
        self.calls = []
        def tester(service):
            node = self.core.proxies[sw.PROBE_GROUP]['now']
            self.calls.append(node)
            return {'ok': self.result[node], 'checks': [], 'checkedAt': self.now}
        self.engine = sw.Watchdog(self.store, self.core, tester, lambda: self.now)

    def live_writes(self):
        return [x for x in self.core.writes if x[0] != sw.PROBE_GROUP]

    def test_three_failures_and_two_candidate_successes_then_only_affected_sessions_close(self):
        self.engine.check('whatsapp')
        self.engine.check('whatsapp')
        self.assertEqual(self.live_writes(), [])
        self.engine.check('whatsapp')
        self.assertEqual(self.calls, ['A', 'A', 'A', 'B', 'B'])
        self.assertEqual(self.live_writes(), [(sw.GROUPS['whatsapp'], 'B')])
        self.assertEqual(self.core.deleted, ['wa-old'])
        self.assertEqual(self.core.proxies[sw.GROUPS['telegram']]['now'], 'A')
        state = self.engine.state['services']['whatsapp']
        self.assertEqual(state['failures'], 0)
        self.assertTrue(state['result']['ok'])

    def test_transient_failure_recovers_without_switch(self):
        self.engine.check('whatsapp')
        self.result['A'] = True
        self.engine.check('whatsapp')
        self.assertEqual(self.engine.state['services']['whatsapp']['failures'], 0)
        self.assertEqual(self.live_writes(), [])

    def test_switch_with_no_active_sessions_does_not_report_cleanup_failure(self):
        original = self.engine.api
        self.engine.api = lambda path, *a, **kw: {'connections': None} if path == '/connections' else original(path, *a, **kw)
        for _ in range(3):
            self.engine.check('whatsapp')
        self.assertEqual([e['kind'] for e in self.engine.state['events']], ['switch'])

    def test_cooldown_survives_restart_and_blocks_churn(self):
        state = self.engine.state['services']['whatsapp']
        state.update(node='A', failures=2, lastSwitchAt=self.now - 1)
        self.engine.save()
        restored = sw.Watchdog(self.store, self.core, self.engine.tester, lambda: self.now)
        restored.state['services']['whatsapp'].update(node='A', failures=2)
        restored.check('whatsapp')
        self.assertEqual(restored.state['services']['whatsapp']['phase'], 'cooldown')
        self.assertEqual(self.live_writes(), [])

    def test_no_healthy_candidate_keeps_live_route(self):
        self.result.update(B=False, C=False)
        self.engine.state['services']['telegram'].update(node='A', failures=2)
        self.engine.check('telegram')
        self.assertEqual(self.live_writes(), [])
        self.assertEqual(self.engine.state['services']['telegram']['phase'], 'unavailable')

    def test_candidate_must_pass_twice(self):
        calls = []
        def tester(service):
            n = self.core.proxies[sw.PROBE_GROUP]['now']
            calls.append(n)
            return {'ok': n == 'B' and calls.count('B') == 1, 'checks': []}
        self.engine.tester = tester
        self.engine.state['services']['telegram'].update(node='A', failures=2)
        self.engine.check('telegram')
        self.assertEqual(self.live_writes(), [])

    def test_exhausted_preferred_nodes_do_not_starve_remaining_candidates(self):
        for node in ['D','E','F','G','H']:
            self.core.proxies[node] = {'type':'Hysteria2','name':node}
            self.result[node] = node == 'H'
        self.result.update(B=False, C=False)
        self.core.proxies[sw.GROUPS['whatsapp']]['all'] = list('ABCDEFGH')
        state = self.engine.state['services']['whatsapp']
        state.update(node='A',failures=2,lastGoodNodes=list('BCDEFG'))
        self.engine.check('whatsapp')
        self.assertEqual(self.live_writes(),[])
        self.engine.check('whatsapp')
        self.assertEqual(self.core.proxies[sw.GROUPS['whatsapp']]['now'],'H')

    def test_external_selection_during_probe_is_not_overwritten(self):
        original = self.engine.tester
        def tester(service):
            if self.core.proxies[sw.PROBE_GROUP]['now'] == 'B':
                self.core.proxies[sw.GROUPS[service]]['now'] = 'C'
            return original(service)
        self.engine.tester = tester
        self.engine.state['services']['whatsapp'].update(node='A', failures=2)
        self.engine.check('whatsapp')
        self.assertEqual(self.core.proxies[sw.GROUPS['whatsapp']]['now'], 'C')
        self.assertEqual(self.live_writes(), [])

    def test_disabling_during_candidate_probe_prevents_commit(self):
        original = self.engine.tester
        def tester(service):
            if self.core.proxies[sw.PROBE_GROUP]['now'] == 'B':
                self.engine.request(service, False)
            return original(service)
        self.engine.tester = tester
        self.engine.state['services']['whatsapp'].update(node='A', failures=2)
        with self.assertRaises(InterruptedError):
            self.engine.check('whatsapp')
        self.assertEqual(self.live_writes(), [])

    def test_general_and_direct_modes_do_not_change_irrelevant_service_selectors(self):
        for mode in ['global', 'direct']:
            self.core.mode = mode
            self.engine.check('telegram')
        self.assertEqual(self.core.writes, [])
        self.assertEqual(self.calls, [])

    def test_channel_failure_is_not_a_node_failure(self):
        self.engine.tester = lambda service: {'ok': False, 'checks': [{'infrastructure': True}]}
        with self.assertRaises(sw.InfrastructureError):
            self.engine.check('whatsapp')
        self.assertEqual(self.engine.state['services']['whatsapp']['failures'], 0)
        self.assertEqual(self.live_writes(), [])

    def test_disabled_policy_and_corrupt_policy_never_switch(self):
        self.engine.request('telegram', False)
        self.engine.check('telegram')
        self.assertEqual(self.core.writes, [])
        self.store.write('service-policy.json', {'telegram': 'true'})
        with self.assertRaises(ValueError):
            self.engine.check('telegram')
        self.assertIn('设置损坏', self.engine.snapshot()['engineError'])

    def test_healthy_nested_auto_route_is_pinned_without_dropping_sessions(self):
        self.core.proxies['auto'] = {'type': 'URLTest', 'all': ['A', 'B'], 'now': 'A'}
        obj = self.core.proxies[sw.GROUPS['whatsapp']]
        obj.update(now='auto', all=['auto', 'A', 'B', 'C'])
        self.result['A'] = True
        self.engine.check('whatsapp')
        self.assertEqual(self.live_writes(), [(sw.GROUPS['whatsapp'], 'A')])
        self.assertEqual(self.core.deleted, [])

    def test_prepare_config_is_idempotent_and_preserves_ports_rules_and_secret(self):
        import yaml
        root = Path(self.temp.name)
        (root / 'controller.secret').write_text('test-only-controller')
        original = {'port': 7892, 'secret': 'test-only-controller', 'rules': ['MATCH,服务'],
                    'proxy-groups': [{'name': '服务', 'type': 'select', 'use': ['airport']}],
                    'proxy-providers': {'airport': {'type': 'http', 'url': 'https://example.com/sub'}}}
        with patch.object(pa, 'ROOT', root):
            text = sw.prepare_config(yaml.safe_dump(original))
            self.assertEqual(text, sw.prepare_config(text))
            data = yaml.safe_load(text)
        self.assertEqual(data['rules'], original['rules'])
        self.assertEqual(data['port'], original['port'])
        self.assertEqual(data['secret'], original['secret'])
        listener = data['listeners'][0]
        self.assertEqual(listener['proxy'], sw.PROBE_GROUP)
        self.assertTrue(listener['users'][0]['password'])
        self.assertTrue(data['proxy-groups'][-1]['hidden'])


class ProtocolTests(unittest.TestCase):
    def test_whatsapp_requires_both_chat_and_verified_media(self):
        with patch.object(sw, 'whatsapp_chat_probe', return_value='chat'), \
             patch.object(sw, 'whatsapp_media_probe', side_effect=ssl_error()):
            result = sw.probe('whatsapp')
        self.assertFalse(result['ok'])
        self.assertEqual([c['ok'] for c in result['checks']], [True, False])

    def test_telegram_response_nonce_is_checked(self):
        class Peer:
            def __init__(self, wrong=False):
                self.wrong = wrong
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def sendall(self, request):
                nonce = b'x' * 16 if self.wrong else request[-16:]
                body = struct.pack('<I', 0x05162463) + nonce + b's' * 28
                data = b'\0' * 8 + struct.pack('<QI', 1, len(body)) + body
                self.data = struct.pack('<I', len(data)) + data
            def recv(self, n):
                data, self.data = self.data[:n], self.data[n:]
                return data
        with patch.object(sw, 'socks_connect', return_value=Peer()):
            self.assertIn('nonce', sw.telegram_probe())
        with patch.object(sw, 'socks_connect', return_value=Peer(True)):
            with self.assertRaises(ConnectionError):
                sw.telegram_probe()

    def test_unreachable_probe_listener_never_falls_back_to_direct(self):
        with patch.object(sw.socket, 'create_connection', side_effect=OSError('unreachable')) as dial:
            with self.assertRaises(sw.InfrastructureError):
                sw.socks_connect('g.whatsapp.net', 5222)
        dial.assert_called_once_with(('mihomo', sw.PROBE_PORT), 6)


def ssl_error():
    import ssl
    return ssl.SSLCertVerificationError('test certificate rejected')
