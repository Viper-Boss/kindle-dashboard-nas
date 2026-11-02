"""Durable proxy accounting. Daily counters come from byte hooks, not active-flow samples."""
from datetime import date, datetime, timedelta
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time
import urllib.request
from zoneinfo import ZoneInfo

DB_PATH = Path(os.getenv('TRAFFIC_DB', '/data/traffic.sqlite3'))
ZONE = ZoneInfo('Asia/Shanghai')
_collector_lock = threading.Lock()
_stop = threading.Event()
_thread = None
_status = {'lastCollectedAt': None, 'errors': {}, 'interval': 10}


class TrafficStore:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY,label TEXT NOT NULL DEFAULT '',first_seen TEXT,last_seen TEXT);
                CREATE TABLE IF NOT EXISTS daily(day TEXT,client TEXT,service TEXT,upload INTEGER NOT NULL,download INTEGER NOT NULL,
                    PRIMARY KEY(day,client,service));
                CREATE TABLE IF NOT EXISTS cursors(source TEXT,epoch TEXT,day TEXT,client TEXT,service TEXT,upload INTEGER,download INTEGER,
                    PRIMARY KEY(source,epoch,day,client,service));
                CREATE INDEX IF NOT EXISTS daily_client ON daily(client,day);
            ''')
            db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)', ('enabledAt', datetime.now(ZONE).isoformat()))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=10000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, source, report, excluded=(), server_ips=()):
        epoch = report.get('epoch')
        rows = report.get('clients')
        if not isinstance(epoch, str) or not epoch or len(epoch) > 128 or not isinstance(rows, list):
            raise ValueError('流量计数接口格式无效')
        if len(rows) > 160000:
            raise ValueError('流量计数条目过多')
        stamp = datetime.now(ZONE).isoformat()
        added = 0
        valid_days = []
        with self.connect() as db:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw_client, day = str(row.get('client', 'unknown')), row.get('day', '')
                try:
                    date.fromisoformat(day)
                except (TypeError, ValueError):
                    continue
                if day > datetime.now(ZONE).date().isoformat() or day < '2020-01-01':
                    continue
                if raw_client in excluded:
                    continue
                client = 'server:dashboard' if raw_client in server_ips else raw_client
                if client not in ('server:dashboard', 'unknown', 'unknown:overflow'):
                    try:
                        addr = ipaddress.ip_address(client)
                        client = str(addr.ipv4_mapped or addr) if isinstance(addr, ipaddress.IPv6Address) else str(addr)
                    except ValueError:
                        continue
                service = row.get('service', 'proxy')
                if service not in ('proxy', 'telegram', 'whatsapp'):
                    continue
                up, down = row.get('upload'), row.get('download')
                if any(type(v) is not int or not 0 <= v <= 2**60 for v in (up, down)):
                    continue
                valid_days.append(day)
                key = (source, epoch, day, raw_client, service)
                old = db.execute('SELECT upload,download FROM cursors WHERE source=? AND epoch=? AND day=? AND client=? AND service=?', key).fetchone()
                if old is not None and up==old['upload'] and down==old['download']:
                    continue
                # Counter rollback is rebased without charging historical bytes twice.
                du = up if old is None else max(0, up - old['upload'])
                dd = down if old is None else max(0, down - old['download'])
                db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?,?,?,?,?,?)', (*key, up, down))
                if not du and not dd:
                    continue
                label = {'server:dashboard':'服务器 · 数据屏','unknown':'未识别来源','unknown:overflow':'其他来源（当日条目上限）'}.get(client,'')
                db.execute('INSERT INTO clients VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen', (client, label, stamp, stamp))
                db.execute('INSERT INTO daily VALUES (?,?,?,?,?) ON CONFLICT(day,client,service) DO UPDATE SET upload=upload+excluded.upload,download=download+excluded.download', (day, client, service, du, dd))
                added += du + dd
            # Old daily history is retained; only temporary observation cursors expire.
            if valid_days:
                cutoff=(date.fromisoformat(max(valid_days))-timedelta(days=45)).isoformat()
                db.execute('DELETE FROM cursors WHERE source=? AND day<?', (source,cutoff))
        return added

    def alias(self, client, label):
        if not isinstance(client, str) or not isinstance(label, str) or len(label) > 60:
            raise ValueError('客户端备注无效（最多 60 字）')
        with self.connect() as db:
            if db.execute('UPDATE clients SET label=? WHERE id=?', (label.strip(), client)).rowcount != 1:
                raise ValueError('客户端不存在')

    def summary(self, granularity='day', period=None, client=None):
        now = datetime.now(ZONE).date()
        if granularity == 'day':
            start = date.fromisoformat((period or now.strftime('%Y-%m'))+'-01')
            end = (start.replace(day=28)+timedelta(days=4)).replace(day=1)
            expr = 'day'
        elif granularity == 'month':
            year = int(period or now.year)
            start, end = date(year,1,1), date(year+1,1,1)
            expr = 'substr(day,1,7)'
        elif granularity == 'year':
            start, end, expr = date(2020,1,1), date(min(now.year+1,9999),1,1), 'substr(day,1,4)'
        else:
            raise ValueError('统计粒度无效')
        where = 'day>=? AND day<?'
        args = [start.isoformat(),end.isoformat()]
        if client:
            where += ' AND client=?';args.append(client)
        with self.connect() as db:
            enabled = db.execute('SELECT value FROM metadata WHERE key="enabledAt"').fetchone()[0]
            series = [dict(x) for x in db.execute(f'SELECT {expr} AS bucket,SUM(upload) upload,SUM(download) download FROM daily WHERE {where} GROUP BY bucket ORDER BY bucket',args)]
            clients = [dict(x) for x in db.execute(f'SELECT d.client id,c.label,c.last_seen,SUM(d.upload) upload,SUM(d.download) download FROM daily d LEFT JOIN clients c ON c.id=d.client WHERE {where.replace("client=?","d.client=?")} GROUP BY d.client ORDER BY SUM(d.upload+d.download) DESC',args)]
            services = [dict(x) for x in db.execute(f'SELECT service,SUM(upload) upload,SUM(download) download FROM daily WHERE {where} GROUP BY service',args)]
            cards = {}
            for key, lower, upper in [('today',now,now+timedelta(days=1)),('month',now.replace(day=1),(now.replace(day=28)+timedelta(days=4)).replace(day=1)),('year',date(now.year,1,1),date(now.year+1,1,1))]:
                row=db.execute('SELECT COALESCE(SUM(upload),0) upload,COALESCE(SUM(download),0) download FROM daily WHERE day>=? AND day<?',(lower.isoformat(),upper.isoformat())).fetchone();cards[key]=dict(row)
            totals = {'upload':sum(x['upload'] for x in series),'download':sum(x['download'] for x in series)}
        return dict(enabledAt=enabled, timezone='Asia/Shanghai', granularity=granularity, period=period, series=series, clients=clients, services=services, totals=totals, cards=cards, collector=dict(_status))

    def backup(self, destination):
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)


def _addresses(name):
    try:
        return {x[4][0] for x in socket.getaddrinfo(name, None)}
    except OSError:
        return set()


def collect_once():
    import proxy_admin
    with _collector_lock:
        store = TrafficStore()
        relay_ips = _addresses('mtproto') | _addresses('nas-mtproto')
        server_ips = _addresses(socket.gethostname())
        errors = {}
        try:
            report = proxy_admin.api('/connections/clients', timeout=5)
            store.ingest('mihomo', report, excluded=relay_ips, server_ips=server_ips)
        except Exception as error:
            errors['proxy'] = '代理内核计数暂不可用（'+type(error).__name__+'）'
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open('http://mtproto:8766/status', timeout=5) as response:
                report = json.load(response)['clientTraffic']
            store.ingest('relay', report)
        except Exception as error:
            errors['relay'] = 'TG/WhatsApp 计数暂不可用（'+type(error).__name__+'）'
        _status.update(lastCollectedAt=datetime.now(ZONE).isoformat(), errors=errors)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    def run():
        while not _stop.is_set():
            try:
                collect_once()
            except Exception as error:
                _status['errors'] = {'storage':'统计保存失败（'+type(error).__name__+'）'}
            _stop.wait(10)
    _thread = threading.Thread(target=run, name='traffic-history', daemon=True)
    _thread.start()


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=12)
    collect_once()
