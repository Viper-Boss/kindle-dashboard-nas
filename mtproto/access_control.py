"""Per-service LAN/WAN access switch shared by the Telegram and WhatsApp relays.

The admin console writes ``access.json`` ({"telegram": {"wan": true}, "whatsapp": {"wan": false}});
this module re-reads it every couple of seconds, so a switch takes effect without a restart.

It is an application-level check on the peer address: when WAN access is off, only private
(LAN / loopback / link-local) peers are served. Router port forwarding and the NAS firewall
remain the real network boundary.
"""
import ipaddress
import json
import os
import time

ACCESS_FILE = os.environ.get('ACCESS_FILE', '/proxy/access.json')
SERVICES = ('telegram', 'whatsapp')
CACHE_SECONDS = 2.0

LAN_NETWORKS = [ipaddress.ip_network(n) for n in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', '169.254.0.0/16',
    '::1/128', 'fc00::/7', 'fe80::/10')]

STATS = {name: {'lan': 0, 'wan': 0, 'blocked': 0} for name in SERVICES}
_cache = {'at': 0.0, 'data': None}


def is_lan(host):
    try:
        ip = ipaddress.ip_address(str(host).split('%')[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in network for network in LAN_NETWORKS)


def _load():
    """Missing file keeps the old behaviour (allowed); an unreadable file fails closed."""
    now = time.monotonic()
    if _cache['data'] is not None and now - _cache['at'] < CACHE_SECONDS:
        return _cache['data']
    try:
        with open(ACCESS_FILE, encoding='utf-8') as handle:
            raw = json.load(handle)
        data = {name: bool((raw.get(name) or {}).get('wan', True)) for name in SERVICES}
    except FileNotFoundError:
        data = {name: True for name in SERVICES}
    except (OSError, ValueError, AttributeError):
        data = {name: False for name in SERVICES}
    _cache.update(at=now, data=data)
    return data


def wan_enabled(service):
    return _load().get(service, False)


def check(service, host):
    """Return True when this peer may use ``service``; keeps per-service counters."""
    stats = STATS.setdefault(service, {'lan': 0, 'wan': 0, 'blocked': 0})
    if is_lan(host):
        stats['lan'] += 1
        return True
    if wan_enabled(service):
        stats['wan'] += 1
        return True
    stats['blocked'] += 1
    return False


def snapshot():
    return {name: dict(STATS[name], wan=wan_enabled(name)) for name in SERVICES}
