#!/usr/bin/env python3
"""Generate local credentials and runtime files without overwriting an existing setup."""
import json
import os
from pathlib import Path
import secrets
import shutil

ROOT = Path(__file__).resolve().parents[1]


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding='utf-8')
        os.chmod(path, 0o600)


def main():
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        raise SystemExit('Run with sudo: runtime secrets must be owned by the root UID used by the read-only proxy container.')
    env = ROOT / '.env'
    if not env.exists():
        shutil.copy2(ROOT / '.env.example', env)
    text = env.read_text(encoding='utf-8')
    if 'ADMIN_PASSWORD=change-me' in text:
        text = text.replace('ADMIN_PASSWORD=change-me', 'ADMIN_PASSWORD=' + secrets.token_urlsafe(24))
        env.write_text(text, encoding='utf-8')
    os.chmod(env, 0o600)
    settings = dict(line.split('=',1) for line in text.splitlines() if '=' in line and not line.lstrip().startswith('#'))
    host = settings.get('NAS_LAN_HOST', '192.168.120.200')
    secret_path = ROOT / 'proxy/controller.secret'
    write_private(secret_path, secrets.token_hex(32))
    template = (ROOT / 'proxy/config.example.yaml').read_text(encoding='utf-8')
    write_private(ROOT / 'proxy/config.yaml', template.replace('CONTROLLER_SECRET', secret_path.read_text().strip()))
    tg_path = ROOT / 'mtproto/config.py'
    write_private(tg_path, (ROOT / 'mtproto/config.example.py').read_text(encoding='utf-8').replace('MTPROTO_SECRET', secrets.token_hex(16)))
    if hasattr(os, 'chown'):
        os.chown(tg_path, 0, 0)
    # FnOS may create bind directories with mode 000 and grant access only
    # through ACLs. The relay drops DAC override capabilities, so its root UID
    # still needs ordinary traversal/read permissions for these mounts.
    for directory in [ROOT / 'proxy', ROOT / 'mtproto',
                      *(p for p in (ROOT / 'mtproto').rglob('*') if p.is_dir())]:
        os.chmod(directory, 0o755)
    for source in (ROOT / 'mtproto').rglob('*.py'):
        os.chmod(source, 0o600 if source == tg_path else 0o644)
    # Read the existing secret so rerunning this script keeps the client link valid.
    import runpy
    tg_secret = runpy.run_path(str(tg_path))['USERS']['lan']
    write_private(ROOT / 'proxy/telegram.json', json.dumps({'server':host,'port':8443,'secret':'dd'+tg_secret,
        'link':'https://t.me/proxy?server='+host+'&port=8443&secret=dd'+tg_secret,'note':'通过 Mihomo 当前节点连接 Telegram'},ensure_ascii=False,indent=2))
    write_private(ROOT / 'proxy/whatsapp.json', json.dumps({'server':host,'chatPort':5222,'mediaPort':7777,'publicHost':''},indent=2))
    (ROOT / 'data').mkdir(exist_ok=True)
    print('Local runtime files ready. Set NAS_BIND_IP and review .env before starting Docker.')
    print('Administrator password is stored in .env; existing credentials were preserved.')


if __name__ == '__main__':
    main()
