"""Service-aware failover using an isolated, authenticated Mihomo probe listener.

Probes never log in to accounts or send messages. Results cover protocol reachability.
"""
import copy
import hashlib
import os
import socket
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import maintenance
import proxy_admin as pa

GROUPS = {'telegram': 'Telegram代理', 'whatsapp': 'WhatsApp代理'}
PROBE_GROUP, PROBE_PORT = '__kindle_service_probe', 17893
INTERVAL, FAILURES, COOLDOWN = 60, 3, 600
_engine = None


class InfrastructureError(RuntimeError):
    """Probe channel failures are not evidence that an outbound node is bad."""


def probe_password():
    secret = (pa.ROOT / 'controller.secret').read_text().strip()
    return hashlib.sha256(('kindle-service-probe:' + secret).encode()).hexdigest()


def prepare_config(text):
    """The probe listener is internal to Docker, with no published host port."""
    import yaml
    if len(text.encode()) > 2_000_000:
        raise ValueError('代理配置过大')
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError('代理配置格式无效')
    groups, listeners = data.setdefault('proxy-groups', []), data.setdefault('listeners', [])
    if not isinstance(groups, list) or not isinstance(listeners, list):
        raise ValueError('代理策略组或监听器格式无效')
    names = [p['name'] for p in data.get('proxies', []) if isinstance(p, dict) and p.get('name')]
    providers = list(data.get('proxy-providers', {}))
    if not names and not providers:
        return text
    group = {'name': PROBE_GROUP, 'type': 'select', 'hidden': True}
    if names:
        group['proxies'] = names
    if providers:
        group['use'] = providers
    listener = {'name': PROBE_GROUP, 'type': 'socks', 'listen': '0.0.0.0',
                'port': PROBE_PORT, 'udp': False, 'proxy': PROBE_GROUP,
                'users': [{'username': 'kindle-probe', 'password': probe_password()}]}
    if (next((g for g in groups if g.get('name') == PROBE_GROUP), None) == group and
            next((g for g in listeners if g.get('name') == PROBE_GROUP), None) == listener):
        return text
    if any(g.get('name') != PROBE_GROUP and g.get('port') == PROBE_PORT for g in listeners):
        raise ValueError('服务检测端口已被其他监听器占用')
    data['proxy-groups'] = [g for g in groups if g.get('name') != PROBE_GROUP] + [group]
    data['listeners'] = [g for g in listeners if g.get('name') != PROBE_GROUP] + [listener]
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def read_exact(sock, size):
    data = b''
    deadline = time.monotonic() + 6
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('协议响应超时')
        if hasattr(sock, 'settimeout'):
            sock.settimeout(remaining)
        part = sock.recv(size - len(data))
        if not part:
            raise ConnectionError('上游在协议握手完成前关闭连接')
        data += part
    return data


def socks_connect(host, port):
    try:
        s = socket.create_connection(('mihomo', PROBE_PORT), 6)
    except OSError:
        raise InfrastructureError('独立检测通道未连接，暂停切换') from None
    s.settimeout(6)
    phase = 'channel'
    try:
        s.sendall(b'\x05\x01\x02')
        if read_exact(s, 2) != b'\x05\x02':
            raise ConnectionError('独立检测通道未提供认证')
        user, password = b'kindle-probe', probe_password().encode()
        s.sendall(b'\x01' + bytes([len(user)]) + user + bytes([len(password)]) + password)
        if read_exact(s, 2) != b'\x01\x00':
            raise ConnectionError('独立检测通道认证失败')
        phase = 'target'
        name = host.encode('idna')
        s.sendall(b'\x05\x01\x00\x03' + bytes([len(name)]) + name + port.to_bytes(2, 'big'))
        head = read_exact(s, 4)
        if head[:2] != b'\x05\x00':
            raise ConnectionError('节点无法连接服务（SOCKS %d）' % head[1])
        size = {1: 4, 4: 16}.get(head[3])
        if head[3] == 3:
            size = read_exact(s, 1)[0]
        if size is None:
            raise ConnectionError('独立检测通道返回无效地址')
        read_exact(s, size + 2)
        return s
    except Exception as error:
        s.close()
        if phase == 'channel':
            raise InfrastructureError('独立检测通道认证或握手失败，暂停切换') from error
        raise


def telegram_probe():
    nonce = os.urandom(16)
    body = struct.pack('<I', 0xbe7e8ef1) + nonce
    payload = b'\0' * 8 + struct.pack('<QI', int(time.time() * (1 << 32)) & ~3, len(body)) + body
    with socks_connect('149.154.167.51', 443) as s:
        s.sendall(b'\xee' * 4 + struct.pack('<I', len(payload)) + payload)
        length = struct.unpack('<I', read_exact(s, 4))[0]
        if not 56 <= length <= 4096:
            raise ConnectionError('Telegram 返回了无效 MTProto 帧')
        response = read_exact(s, length)
        if (response[:8] != b'\0' * 8 or
                struct.unpack('<I', response[16:20])[0] != length - 20 or
                response[20:24] != struct.pack('<I', 0x05162463) or response[24:40] != nonce):
            raise ConnectionError('Telegram MTProto resPQ / nonce 校验失败')
    return 'MTProto resPQ / nonce 已验证（DC2）'


def whatsapp_chat_probe():
    # RFC 7748 public test vector. There is no account or private key in this probe.
    public = bytes.fromhex('e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c')
    hello = b'\x12\x22\x0a\x20' + public
    with socks_connect('g.whatsapp.net', 5222) as s:
        s.sendall(b'PROXY TCP4 192.168.120.200 0.0.0.0 54321 5222\r\n' +
                  b'WA\x06\x03' + len(hello).to_bytes(3, 'big') + hello)
        size = int.from_bytes(read_exact(s, 3), 'big')
        if not 32 < size < 8192:
            raise ConnectionError('WhatsApp 返回了无效 Noise 帧')
        response = read_exact(s, size)
        if response[0] != 0x1a or b'\x0a\x20' not in response[:8]:
            raise ConnectionError('WhatsApp 未返回有效 Noise ServerHello')
    return '聊天 Noise ServerHello 已验证'


def whatsapp_media_probe():
    with socks_connect('whatsapp.net', 443) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname='mmg.whatsapp.net') as s:
            return '媒体 %s / 证书已验证' % s.version()


def probe(service):
    start = time.monotonic()
    funcs = [telegram_probe] if service == 'telegram' else [whatsapp_chat_probe, whatsapp_media_probe]
    with ThreadPoolExecutor(max_workers=len(funcs)) as pool:
        futures = [pool.submit(f) for f in funcs]
        checks = []
        for future in futures:
            try:
                checks.append({'ok': True, 'detail': future.result()})
            except Exception as e:
                checks.append({'ok': False, 'infrastructure': isinstance(e, InfrastructureError),
                               'detail': str(e)[:180] or type(e).__name__})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks,
            'durationMs': round((time.monotonic() - start) * 1000), 'checkedAt': time.time()}


def resolve(proxies, name):
    seen = set()
    while name in proxies and name not in seen:
        seen.add(name)
        obj = proxies[name]
        if 'all' not in obj:
            return name if obj.get('type') not in ('Direct', 'Reject', 'Compatible') else None
        name = obj.get('now')
    return None


class Watchdog:
    def __init__(self, store=None, api=None, tester=None, clock=None):
        self.store = store or maintenance.SharedStore()
        self.api, self.tester, self.clock = api or pa.api, tester or probe, clock or time.time
        self.lock = threading.RLock()
        self.stop_event, self.wake = threading.Event(), threading.Event()
        saved = self.store.read('service-health.json', {}) or {}
        self.state = {'services': {}, 'events': saved.get('events', [])[:40], 'engineError': '',
                      'interval': INTERVAL, 'failureThreshold': FAILURES, 'cooldownSeconds': COOLDOWN}
        for service in GROUPS:
            previous = (saved.get('services') or {}).get(service, {})
            self.state['services'][service] = {
                'phase': 'waiting', 'failures': 0, 'lastSwitchAt': previous.get('lastSwitchAt', 0),
                'lastGoodNodes': previous.get('lastGoodNodes', [])[:8], 'generation': 0,
                'candidateCursor': 0, 'nextCheckAt': self.clock()}

    def policies(self):
        data = self.store.read('service-policy.json', {s: True for s in GROUPS})
        if not isinstance(data, dict) or any(type(data.get(s)) is not bool for s in GROUPS):
            raise ValueError('自动检测设置损坏，已暂停切换')
        return data

    def save(self):
        with self.lock:
            self.store.write('service-health.json', self.state)

    def snapshot(self):
        with self.lock:
            data = copy.deepcopy(self.state)
        try:
            data['enabled'] = self.policies()
        except ValueError as error:
            data.update(enabled={s: False for s in GROUPS}, engineError=str(error))
        data['running'] = bool(getattr(self, 'thread', None) and self.thread.is_alive())
        return data

    def request(self, service, enabled=None):
        if service not in GROUPS:
            raise ValueError('服务不存在')
        with self.lock:
            if enabled is not None:
                if type(enabled) is not bool:
                    raise ValueError('自动检测开关无效')
                policy = self.policies()
                policy[service] = enabled
                self.store.write('service-policy.json', policy)
                self.state['services'][service]['generation'] += 1
                self.state['services'][service].update(phase='waiting' if enabled else 'disabled', testingNode=None)
            self.state['services'][service]['nextCheckAt'] = 0
            self.wake.set()

    def manual_change(self, service):
        with self.lock:
            state = self.state['services'][service]
            state.update(generation=state['generation'] + 1, failures=0, nextCheckAt=0)
            self.wake.set()

    def event(self, service, kind, **values):
        with self.lock:
            self.state['events'].insert(0, dict(service=service, kind=kind, time=self.clock(), **values))
            del self.state['events'][40:]

    def candidates(self, proxies, obj, state, current):
        names = [n for n in obj.get('all', []) if resolve(proxies, n) == n and n != current]
        preferred = [n for n in state['lastGoodNodes'] if n in names]
        rest = [n for n in names if n not in preferred]
        offset = state['candidateCursor'] % max(len(rest), 1)
        return (preferred + rest[offset:] + rest[:offset])[:6]

    def check_node(self, service, node, generation):
        with self.lock:
            if self.state['services'][service]['generation'] != generation or not self.policies()[service]:
                raise InterruptedError('用户已修改设置，本轮检测已取消')
            self.state['services'][service].update(phase='checking', testingNode=node)
        self.api('/proxies/' + pa.quoted(PROBE_GROUP), 'PUT', {'name': node})
        result = self.tester(service)
        if any(c.get('infrastructure') for c in result.get('checks', [])):
            raise InfrastructureError('独立检测通道异常，保留正式出口')
        return result

    def check(self, service):
        with self.lock:
            state = self.state['services'][service]
            if not self.policies()[service]:
                state.update(phase='disabled')
                return
            generation = state['generation']
        if self.api('/configs').get('mode') != 'rule':
            with self.lock:
                state.update(phase='paused', reason='当前不是规则模式，服务专用出口不生效')
            return
        proxies = self.api('/proxies')['proxies']
        for provider in self.api('/providers/proxies').get('providers', {}).values():
            for node in provider.get('proxies', []):
                proxies.setdefault(node['name'], node)
        obj = proxies.get(GROUPS[service], {})
        if obj.get('type') != 'Selector' or PROBE_GROUP not in proxies:
            raise RuntimeError('缺少服务专用策略组或独立检测通道')
        current = resolve(proxies, obj.get('now'))
        if not current:
            raise RuntimeError('当前服务没有可检测的代理节点')
        with self.lock:
            if state.get('node') != current:
                state.update(node=current, failures=0)
            state.update(candidateIndex=0, candidateTotal=0)
        result = self.check_node(service, current, generation)
        with self.lock:
            if state['generation'] != generation:
                return
            state.update(result=result, lastCheckAt=self.clock(), testingNode=None)
            state['failures'] = 0 if result['ok'] else state['failures'] + 1
        if result['ok']:
            with self.lock:
                state.update(phase='healthy', reason='当前节点服务检测通过')
                state['lastGoodNodes'] = ([current] + [n for n in state['lastGoodNodes'] if n != current])[:8]
            if obj.get('now') != current:
                self.commit(service, obj['now'], current, generation, pin=True)
            return
        if state['failures'] < FAILURES:
            with self.lock:
                state.update(phase='retry', reason='连续失败达到 3 次才切换')
            return
        if self.clock() - state['lastSwitchAt'] < COOLDOWN:
            with self.lock:
                state.update(phase='cooldown', reason='切换冷却中，继续检测但暂不换线')
            return
        names = self.candidates(proxies, obj, state, current)
        for index, name in enumerate(names):
            if self.stop_event.is_set():
                return
            with self.lock:
                state.update(candidateIndex=index + 1, candidateTotal=len(names))
            first = self.check_node(service, name, generation)
            second = self.check_node(service, name, generation) if first['ok'] else first
            with self.lock:
                state['candidateResult'] = dict(node=name, **second)
                state['candidateCursor'] += 1
            if first['ok'] and second['ok']:
                self.commit(service, obj['now'], name, generation)
                return
        with self.lock:
            state.update(phase='unavailable', testingNode=None,
                         reason='本轮未找到通过复测的备用节点，保留原出口，下轮继续寻找')
        self.event(service, 'no-candidate', node=current)

    def commit(self, service, expected, node, generation, pin=False):
        with pa.LOCK, self.lock:
            state = self.state['services'][service]
            if state['generation'] != generation or not self.policies()[service]:
                return False
            fresh = self.api('/proxies/' + pa.quoted(GROUPS[service]))
            if fresh.get('now') != expected or node not in fresh.get('all', []):
                state.update(phase='waiting', reason='其他控制端已换线，下轮重新检测', failures=0)
                return False
            self.api('/proxies/' + pa.quoted(GROUPS[service]), 'PUT', {'name': node})
            state.update(node=node, failures=0, phase='healthy', testingNode=None,
                         reason='已固定当前可用节点' if pin else '已切换到通过两次检测的备用节点')
            state['lastGoodNodes'] = ([node] + [n for n in state['lastGoodNodes'] if n != node])[:8]
            if not pin:
                state['lastSwitchAt'] = self.clock()
                state['result'] = dict(state.get('candidateResult') or {})
                state['lastCheckAt'] = state['result'].get('checkedAt', self.clock())
            self.event(service, 'pin' if pin else 'switch', previous=expected, node=node)
        if not pin:
            try:
                for c in (self.api('/connections').get('connections') or []):
                    if GROUPS[service] in c.get('chains', []) and node not in c.get('chains', []):
                        self.api('/connections/' + pa.quoted(c['id']), 'DELETE')
            except Exception:
                self.event(service, 'reconnect-warning', node=node)
        return True

    def ensure_channel(self):
        with pa.LOCK:
            source = (pa.ROOT / 'config.yaml').read_text()
            new = prepare_config(source)
            if new != source:
                selected = {n: p.get('now') for n, p in self.api('/proxies')['proxies'].items()
                            if p.get('type') == 'Selector' and n != PROBE_GROUP}
                pa.save_and_reload(new)
                for group, node in selected.items():
                    fresh = self.api('/proxies/' + pa.quoted(group))
                    if node in fresh.get('all', []):
                        self.api('/proxies/' + pa.quoted(group), 'PUT', {'name': node})

    def loop(self):
        while not self.stop_event.is_set():
            try:
                worker = self.store.read('status.json', {}) or {}
                if worker.get('busy') and self.clock() - worker.get('heartbeat', 0) < 90:
                    with self.lock:
                        self.state['engineError'] = '维护任务执行中，服务检测暂缓'
                else:
                    self.ensure_channel()
                    if not pa.whatsapp_info().get('running'):
                        raise RuntimeError('本机转发服务未连接；暂停节点切换')
                    with self.lock:
                        self.state['engineError'] = ''
                    for service in GROUPS:
                        if self.stop_event.is_set():
                            break
                        with self.lock:
                            due = self.state['services'][service]['nextCheckAt'] <= self.clock()
                        if due:
                            try:
                                self.check(service)
                            except InterruptedError:
                                pass
                            except Exception as e:
                                with self.lock:
                                    self.state['services'][service].update(phase='error', reason=str(e)[:180])
                            finally:
                                with self.lock:
                                    self.state['services'][service]['nextCheckAt'] = self.clock() + INTERVAL
                                self.save()
            except Exception as e:
                with self.lock:
                    self.state['engineError'] = str(e)[:180]
            self.wake.wait(2)
            self.wake.clear()


def start():
    global _engine
    if _engine:
        return
    _engine = Watchdog()
    _engine.thread = threading.Thread(target=_engine.loop, name='service-watchdog', daemon=True)
    _engine.thread.start()


def stop():
    if _engine:
        _engine.stop_event.set()
        _engine.wake.set()
        _engine.thread.join(timeout=12)
        _engine.save()


def snapshot():
    return _engine.snapshot() if _engine else {'running': False, 'services': {}, 'enabled': {},
                                             'events': [], 'engineError': '自动检测尚未启动'}


def request(service, enabled=None):
    if not _engine:
        raise ValueError('自动检测尚未启动')
    _engine.request(service, enabled)
    return {'ok': True, 'queued': True}

def hourly_summary(proxies):
    """Configuration and observed results, never a fabricated kernel progress meter."""
    import yaml
    try:
        text = (pa.ROOT / 'config.yaml').read_text()
        if len(text.encode()) > 2_000_000:
            raise ValueError()
        config = yaml.safe_load(text)
        group = next(g for g in config.get('proxy-groups', []) if g.get('name') == '自动选择')
        current = proxies.get('自动选择', {})
        url = current.get('testUrl') or group.get('url')
        records = []
        for name in current.get('all', []):
            obj = proxies.get(name, {})
            history = (obj.get('extra', {}).get(url) or {}).get('history') or obj.get('history') or []
            records.append(history[-1] if history else {})
        times = [r.get('time') for r in records if r.get('time')]
        return {'interval': group.get('interval'), 'lazy': group.get('lazy', True),
                'timeout': group.get('timeout', 5000), 'tolerance': group.get('tolerance', 0),
                'url': url, 'expectedStatus': current.get('expectedStatus'),
                'current': current.get('now'), 'total': len(records),
                'tested': sum('delay' in r for r in records),
                'available': sum((r.get('delay') or 0) > 0 for r in records),
                'latestSampleAt': max(times) if times else None,
                'generalFollowsAuto': proxies.get('数据屏代理', {}).get('now') == '自动选择'}
    except Exception:
        return {'error': '无法读取自动测速规则'}
