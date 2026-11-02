"""Per-client daily wire counters shared by Telegram and WhatsApp, without polling flows."""
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import ipaddress
import threading
import time
import uuid

EPOCH = uuid.uuid4().hex
STARTED_AT = time.time()
ZONE = timezone(timedelta(hours=8), 'Asia/Shanghai')
_lock = threading.Lock()
_rows = {}
_counts = defaultdict(int)
_last_day = ''


def record(service, client, upload=0, download=0, now=None):
    global _last_day
    if not upload and not download:
        return
    try:
        ip = ipaddress.ip_address(str(client).split('%')[0])
        client = str(ip.ipv4_mapped or ip) if isinstance(ip, ipaddress.IPv6Address) else str(ip)
    except ValueError:
        client = 'unknown'
    stamp = datetime.fromtimestamp(time.time() if now is None else now, ZONE)
    day = stamp.date().isoformat()
    with _lock:
        if _last_day != day:
            cutoff = (stamp.date() - timedelta(days=34)).isoformat()
            for key in list(_rows):
                if key[0] < cutoff:
                    del _rows[key]
            for old in list(_counts):
                if old < cutoff:
                    del _counts[old]
            _last_day = day
        if (day, client, service) not in _rows and _counts[day] >= 4096:
            client = 'unknown:overflow'
        key = day, client, service
        if key not in _rows:
            _rows[key] = [0, 0]
            _counts[day] += 1
        _rows[key][0] += max(0, int(upload))
        _rows[key][1] += max(0, int(download))


def snapshot():
    with _lock:
        rows = [dict(day=d, client=c, service=s, upload=v[0], download=v[1])
                for (d, c, s), v in _rows.items()]
    return dict(epoch=EPOCH, startedAt=STARTED_AT, clients=rows)


class CountingReader:
    def __init__(self, reader, service, client):
        self._reader, self._service, self._client = reader, service, client

    def __getattr__(self, key):
        return getattr(self._reader, key)

    async def _read(self, method, *args):
        try:
            data = await getattr(self._reader, method)(*args)
        except asyncio.IncompleteReadError as error:
            record(self._service, self._client, upload=len(error.partial))
            raise
        record(self._service, self._client, upload=len(data))
        return data

    async def read(self, n=-1):
        return await self._read('read', n)

    async def readexactly(self, n):
        return await self._read('readexactly', n)

    async def readuntil(self, separator=b'\n'):
        return await self._read('readuntil', separator)

    async def readline(self):
        return await self._read('readline')


class CountingWriter:
    def __init__(self, writer, service, client):
        self._writer, self._service, self._client = writer, service, client

    def __getattr__(self, key):
        return getattr(self._writer, key)

    def write(self, data):
        result = self._writer.write(data)
        record(self._service, self._client, download=len(data))
        return result

    def writelines(self, lines):
        for line in lines:
            self.write(line)


def wrap(service, peer, reader, writer):
    client = peer[0] if peer else 'unknown'
    return CountingReader(reader, service, client), CountingWriter(writer, service, client)
