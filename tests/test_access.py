"""Tests for the per-service WAN switch and the dedicated Telegram exit group."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'mtproto'))
sys.path.insert(0, str(ROOT))
import access_control as ac  # noqa: E402
import proxy_admin as pa  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class AccessControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / 'access.json'
        for p in (patch.object(ac, 'ACCESS_FILE', str(self.file)), patch.object(ac, 'CACHE_SECONDS', 0)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        for stats in ac.STATS.values():
            stats.update(lan=0, wan=0, blocked=0)

    def write(self, telegram, whatsapp):
        self.file.write_text(json.dumps({'telegram': {'wan': telegram}, 'whatsapp': {'wan': whatsapp}}))

    def test_lan_detection(self):
        for host in ('192.168.1.5', '10.0.0.9', '172.20.1.1', '127.0.0.1', '::1', 'fe80::1', '::ffff:192.168.1.5'):
            self.assertTrue(ac.is_lan(host), host)
        for host in ('8.8.8.8', '2408:8000::1', '::ffff:8.8.8.8', 'not-an-ip', ''):
            self.assertFalse(ac.is_lan(host), host)

    def test_missing_file_keeps_previous_behaviour(self):
        self.assertTrue(ac.check('telegram', '8.8.8.8'))
        self.assertTrue(ac.check('whatsapp', '8.8.8.8'))

    def test_wan_off_blocks_public_but_not_lan(self):
        self.write(False, False)
        self.assertFalse(ac.check('telegram', '8.8.8.8'))
        self.assertTrue(ac.check('telegram', '192.168.120.7'))
        self.assertEqual(ac.STATS['telegram'], {'lan': 1, 'wan': 0, 'blocked': 1})

    def test_switches_are_independent(self):
        self.write(False, True)
        self.assertFalse(ac.check('telegram', '8.8.8.8'))
        self.assertTrue(ac.check('whatsapp', '8.8.8.8'))
        self.write(True, False)
        self.assertTrue(ac.check('telegram', '8.8.8.8'))
        self.assertFalse(ac.check('whatsapp', '8.8.8.8'))

    def test_corrupt_file_fails_closed(self):
        self.file.write_text('{not json')
        self.assertFalse(ac.check('telegram', '8.8.8.8'))
        self.assertTrue(ac.check('telegram', '192.168.1.2'))

    def test_invalid_switch_types_fail_closed(self):
        for raw in ([], None, {'telegram': {'wan': 'false'}},
                    {'telegram': {'wan': 1}}, {'telegram': True}):
            self.file.write_text(json.dumps(raw))
            self.assertFalse(ac.check('telegram', '8.8.8.8'), raw)

    def test_bridge_proxy_cannot_bypass_wan_switch(self):
        self.write(False, False)
        with patch.object(ac, 'bridge_gateways', return_value={'172.18.0.1'}):
            for host in ('172.18.0.1', '::ffff:172.18.0.1'):
                self.assertFalse(ac.check('telegram', host))
                self.assertFalse(ac.check('whatsapp', host))
            self.assertTrue(ac.check('telegram', '192.168.120.111'))
            self.write(True, False)
            self.assertTrue(ac.check('telegram', '172.18.0.1'))
            self.assertFalse(ac.check('whatsapp', '172.18.0.1'))


class ProxyAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(pa, 'ROOT', Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_wan_action_writes_only_the_requested_service(self):
        self.assertEqual(pa.read_access(), {'telegram': True, 'whatsapp': True})
        pa.action({'action': 'wan-access', 'service': 'telegram', 'enabled': False})
        self.assertEqual(pa.read_access(), {'telegram': False, 'whatsapp': True})
        pa.action({'action': 'wan-access', 'service': 'whatsapp', 'enabled': False})
        pa.action({'action': 'wan-access', 'service': 'telegram', 'enabled': True})
        self.assertEqual(pa.read_access(), {'telegram': True, 'whatsapp': False})

    def test_wan_action_rejects_bad_input(self):
        for bad in ({'service': 'ssh', 'enabled': True}, {'service': 'telegram', 'enabled': 'yes'}, {'service': 'telegram'}):
            with self.assertRaises(ValueError):
                pa.action(dict(bad, action='wan-access'))

    def test_corrupt_access_file_reads_as_closed(self):
        (Path(self.tmp.name) / 'access.json').write_text('oops')
        self.assertEqual(pa.read_access(), {'telegram': False, 'whatsapp': False})

    def test_telegram_public_host_validation_and_link(self):
        with self.assertRaises(ValueError):
            pa.action({'action': 'telegram-settings', 'publicHost': 'https://x.com'})  # 未部署也应先拒绝非法值
        (Path(self.tmp.name) / 'telegram.json').write_text(json.dumps({'server': '192.168.1.2', 'port': 8443, 'secret': 'ddab'}))
        pa.action({'action': 'telegram-settings', 'publicHost': 'tg.example.net'})
        info = json.loads((Path(self.tmp.name) / 'telegram.json').read_text())
        self.assertEqual(info['publicHost'], 'tg.example.net')

    @unittest.skipIf(yaml is None, 'PyYAML not installed')
    def test_add_telegram_group_to_existing_config(self):
        import app
        base = app.mihomo_subscription_config('https://example.com/sub').replace('  - name: Telegram代理', '  - name: Unused', 1)
        base = base.split('rules:')[0] + 'rules:\n  - DOMAIN-SUFFIX,whatsapp.net,WhatsApp代理\n  - MATCH,数据屏代理\n'
        new, changed = pa.add_telegram_group(base)
        self.assertTrue(changed)
        data = yaml.safe_load(new)
        self.assertIn('Telegram代理', [g['name'] for g in data['proxy-groups']])
        telegram_rules = [r for r in data['rules'] if r.endswith('Telegram代理') or 'Telegram代理' in r]
        self.assertEqual(len(telegram_rules), 3 + len(pa.TELEGRAM_CIDRS))
        self.assertEqual(data['rules'][-1], 'MATCH,数据屏代理')           # 兜底规则仍在最后
        self.assertIn('IP-CIDR,149.154.160.0/20,Telegram代理,no-resolve', data['rules'])
        self.assertFalse(pa.add_telegram_group(new)[1])                    # 重复添加不会再改

    @unittest.skipIf(yaml is None, 'PyYAML not installed')
    def test_add_telegram_group_preserves_unindented_yaml_lists(self):
        for whatsapp in (True, False):
            groups = [{'name': '数据屏代理', 'type': 'select', 'proxies': ['DIRECT']}]
            if whatsapp:
                groups.append({'name': 'WhatsApp代理', 'type': 'select', 'proxies': ['DIRECT']})
            original = {'proxy-groups': groups, 'rules': ['MATCH,数据屏代理']}
            text = yaml.safe_dump(original, allow_unicode=True, sort_keys=False)
            result, changed = pa.add_telegram_group(text)
            self.assertTrue(changed)
            parsed = yaml.safe_load(result)
            self.assertIn('Telegram代理', [g['name'] for g in parsed['proxy-groups']])
            self.assertEqual(parsed['rules'][-1], 'MATCH,数据屏代理')
            self.assertFalse(pa.add_telegram_group(result)[1])

    @unittest.skipIf(yaml is None, 'PyYAML not installed')
    def test_templates_define_the_group_and_rules(self):
        import app
        for text in (app.mihomo_subscription_config('https://example.com/sub'),
                     (ROOT / 'proxy/config.example.yaml').read_text(encoding='utf-8')):
            data = yaml.safe_load(text)
            self.assertIn('Telegram代理', [g['name'] for g in data['proxy-groups']])
            self.assertEqual(data['rules'][-1], 'MATCH,数据屏代理')
            self.assertEqual(sum('Telegram代理' in r for r in data['rules']), 3 + len(pa.TELEGRAM_CIDRS))


if __name__ == '__main__':
    unittest.main()
