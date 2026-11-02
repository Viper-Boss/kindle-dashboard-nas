#!/usr/bin/env python3
"""Add scoped proxy rules through the existing local 1Panel agent API."""
import argparse
import http.client
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import time


class Agent(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/etc/1panel/agent.sock')


def api(path, data):
    conn = Agent('localhost', timeout=15)
    conn.request('POST', '/api/v2/hosts/firewall/' + path, json.dumps(data), {'Content-Type':'application/json'})
    result = json.loads(conn.getresponse().read())
    conn.close()
    if result.get('code') != 200:
        raise RuntimeError(str(result))
    return result.get('data')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lan', default='192.168.0.0/16')
    parser.add_argument('--public-whatsapp', action='store_true', help='Allow WhatsApp ports from all IPv4 sources')
    args = parser.parse_args()
    backup = Path(__file__).resolve().parents[1] / 'backups' / ('firewall-' + time.strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    shutil.copytree('/opt/1panel/firewall', backup / '1panel-firewall')
    source=sqlite3.connect('/opt/1panel/db/agent.db')
    dest=sqlite3.connect(backup / '1panel-agent.db')
    source.backup(dest); source.close(); dest.close()
    existing = api('filter/rule/search', {'page':1,'pageSize':1000,'type':'1PANEL_BASIC'})['items']
    rules = [('tcp',p,args.lan) for p in (7890,7891,7892,8443,8787)] + [('udp',7891,args.lan)]
    rules += [('tcp',p,'' if args.public_whatsapp else args.lan) for p in (5222,7777)]
    for proto, port, source in rules:
        if any(r['protocol']==proto and str(r['dstPort'])==str(port) and r['srcIP']==source and r['strategy']=='accept' for r in existing):
            continue
        api('port', {'operation':'add','chain':'1PANEL_BASIC','address':source,'port':str(port),'protocol':proto,
                     'strategy':'accept','description':'Kindle proxy ' + ('WAN' if not source else 'LAN')})
        print('Allowed', proto, port, source or 'all IPv4 sources')
    print('Backup:', backup)
    print('IPv6 and router/cloud firewalls must be checked separately; this script does not change them.')


if __name__ == '__main__':
    main()
