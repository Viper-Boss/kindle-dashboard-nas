#!/usr/bin/env python3
"""NAS-owner recovery: revert web credentials to the private .env bootstrap account."""
import argparse
import os
from pathlib import Path
import sys

parser=argparse.ArgumentParser()
parser.add_argument('--project',default=str(Path(__file__).resolve().parents[1]))
parser.add_argument('--use-env',action='store_true',required=True)
args=parser.parse_args()
if os.geteuid()!=0:raise SystemExit('请在 NAS 使用 sudo 执行账号恢复')
project=Path(args.project).resolve();sys.path.insert(0,str(project))
import web_auth
web_auth.DB_PATH=project/'data/auth.sqlite3'
web_auth.reset_to_environment()
print('已恢复使用私有 .env 中的初始化账号，所有登录会话已退出。')
