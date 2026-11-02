"""Fixed-destination WhatsApp TCP relay, running in the MTProto event loop.

Matches WhatsApp/proxy's chat and media backends. Only WhatsApp destinations
are reachable; this is not an open HTTP/SOCKS proxy. No extra dependencies.
"""
import asyncio
import errno
import ipaddress
import json
import socket
import time

STATE = {'enabled': True, 'startedAt': None, 'active': 0, 'accepted': 0,
         'upstreamConnected': 0, 'failed': 0, 'uploadBytes': 0,
         'downloadBytes': 0, 'lastError': '', 'lastConnectedAt': None}
MAX_CONNECTIONS = 256
IDLE_TIMEOUT = 300


async def connect_socks(host, port, proxy_host='mihomo', proxy_port=7891):
    """Use remote DNS and avoid MTProxy's global PySocks socket patch."""
    loop = asyncio.get_running_loop()
    addresses = await loop.getaddrinfo(proxy_host, proxy_port, family=socket.AF_INET,
                                      type=socket.SOCK_STREAM)
    original_socket = getattr(socket, 'origsocket', socket.socket)
    sock = original_socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    writer = None
    try:
        await asyncio.wait_for(loop.sock_connect(sock, addresses[0][4]), 10)
        reader, writer = await asyncio.open_connection(sock=sock)
        writer.write(b'\x05\x01\x00')
        await writer.drain()
        if await asyncio.wait_for(reader.readexactly(2), 10) != b'\x05\x00':
            raise ConnectionError('Mihomo SOCKS5 authentication failed')
        name = host.encode('idna')
        writer.write(b'\x05\x01\x00\x03' + bytes([len(name)]) + name + port.to_bytes(2, 'big'))
        await writer.drain()
        response = await asyncio.wait_for(reader.readexactly(4), 15)
        if response[:2] != b'\x05\x00':
            raise ConnectionError('Mihomo could not connect to WhatsApp (SOCKS code %d)' % response[1])
        atyp = response[3]
        if atyp == 1:
            length = 4
        elif atyp == 4:
            length = 16
        elif atyp == 3:
            length = (await reader.readexactly(1))[0]
        else:
            raise ConnectionError('Invalid SOCKS5 address response')
        await asyncio.wait_for(reader.readexactly(length + 2), 10)
        return reader, writer
    except BaseException:
        if writer:
            writer.close()
        else:
            sock.close()
        raise


def proxy_header(peer):
    """Same PROXY v1 prefix that the official HAProxy chat backend sends."""
    try:
        ip = ipaddress.ip_address(peer[0])
        family, dest = ('TCP6', '::') if ip.version == 6 else ('TCP4', '0.0.0.0')
        return ('PROXY %s %s %s %s 5222\r\n' % (family, ip, dest, peer[1])).encode('ascii')
    except (ValueError, TypeError, IndexError):
        return b'PROXY UNKNOWN\r\n'


async def pump(reader, writer, direction):
    while True:
        data = await asyncio.wait_for(reader.read(65536), IDLE_TIMEOUT)
        if not data:
            if writer.can_write_eof():
                try:
                    writer.write_eof()
                    await writer.drain()
                except OSError as error:
                    if error.errno not in (errno.ENOTCONN, errno.EPIPE, errno.ECONNRESET):
                        raise
            return
        writer.write(data)
        await writer.drain()
        STATE[direction] += len(data)


async def relay(reader, writer, role):
    if STATE['active'] >= MAX_CONNECTIONS:
        writer.close()
        return
    STATE['active'] += 1
    STATE['accepted'] += 1
    upstream = None
    tasks = []
    try:
        target, port = ('g.whatsapp.net', 5222) if role == 'chat' else ('whatsapp.net', 443)
        incoming, upstream = await connect_socks(target, port)
        if role == 'chat':
            upstream.write(proxy_header(writer.get_extra_info('peername')))
            await upstream.drain()
        STATE['upstreamConnected'] += 1
        STATE['lastConnectedAt'] = time.time()
        STATE['lastError'] = ''
        tasks = [asyncio.create_task(pump(reader, upstream, 'uploadBytes')),
                 asyncio.create_task(pump(incoming, writer, 'downloadBytes'))]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
        for task in pending:
            task.cancel()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        STATE['failed'] += 1
        STATE['lastError'] = str(error)[:200] or type(error).__name__
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for stream in (upstream, writer):
            if stream:
                stream.close()
                try:
                    await asyncio.wait_for(stream.wait_closed(), 3)
                except Exception:
                    pass
        STATE['active'] -= 1


async def health(reader, writer):
    try:
        request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
        path = request.split(b' ', 2)[1]
        if path != b'/status':
            writer.write(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        else:
            payload = json.dumps(STATE).encode()
            writer.write(('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(payload)).encode() + payload)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


def create_servers(loop):
    servers = []
    try:
        for port, role in [(5222, 'chat'), (7777, 'media')]:
            async def handle(reader, writer, role=role):
                await relay(reader, writer, role)
            servers.append(loop.run_until_complete(asyncio.start_server(handle, '0.0.0.0', port)))
        # Docker-internal status only: this port is never published to the host.
        servers.append(loop.run_until_complete(asyncio.start_server(health, '0.0.0.0', 8766)))
        STATE['startedAt'] = time.time()
        print('WhatsApp relay ready: chat=5222 media=7777 (same MTProto process)', flush=True)
        return servers
    except Exception:
        for server in servers:
            server.close()
        raise
