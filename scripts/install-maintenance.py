#!/usr/bin/env python3
"""Install this project's fixed-purpose updater on a systemd NAS."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--project',default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--current-commit')
    parser.add_argument('--state', help='私有状态目录，默认位于项目数据盘 security-staging/maintenance-private')
    parser.add_argument('--seed-builder')
    parser.add_argument('--seed-trivy')
    parser.add_argument('--seed-cache')
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('请使用 sudo 安装 NAS 维护服务')
    project=Path(args.project).resolve()
    if not (project/'.env').is_file() or not (project/'scripts/maintenance_worker.py').is_file():
        raise SystemExit('项目目录缺少运行配置或维护程序')
    state=Path(args.state).resolve() if args.state else project/'security-staging/maintenance-private'
    if any(state.is_relative_to(project/name) for name in ('data','proxy','mtproto')):
        raise SystemExit('维护状态不能放入 Web 或转发容器的共享目录')
    state.mkdir(parents=True,exist_ok=True);state.chmod(0o700)
    if args.current_commit:
        if not re.fullmatch(r'[0-9a-f]{40}',args.current_commit):raise SystemExit('提交编号无效')
        (state/'current.json').write_text(json.dumps({'commit':args.current_commit}))
    if args.seed_builder:
        if not re.fullmatch(r'[a-zA-Z0-9_./:@-]+',args.seed_builder):raise SystemExit('构建缓存引用无效')
        subprocess.run(['docker','image','inspect',args.seed_builder],check=True,stdout=subprocess.DEVNULL)
        (state/'build-cache.json').write_text(json.dumps({'image':args.seed_builder}))
    if args.seed_trivy:
        scanner=Path(args.seed_trivy).resolve()
        version=subprocess.check_output([str(scanner),'--version'],text=True)
        if 'Version: 0.76.1' not in version:raise SystemExit('扫描器版本不匹配')
        (state/'scanner').mkdir(exist_ok=True)
        shutil.copy2(scanner,state/'scanner/trivy');(state/'scanner/trivy').chmod(0o755)
    if args.seed_cache and not (state/'trivy-cache').exists():
        shutil.copytree(args.seed_cache,state/'trivy-cache')
    unit='''[Unit]
Description=Kindle NAS verified GitHub updater
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 {script} --project {project} --state {state}
Restart=on-failure
RestartSec=15
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
UMask=0077

[Install]
WantedBy=multi-user.target
'''.format(script=json.dumps(str(project/'scripts/maintenance_worker.py')),project=json.dumps(str(project)),state=json.dumps(str(state)))
    Path('/etc/systemd/system/kindle-maintenance.service').write_text(unit)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','kindle-maintenance.service'],check=True)
    print('Kindle NAS 维护服务已安装：仅从固定 GitHub main 同步通过检查的版本。')


if __name__=='__main__':main()
