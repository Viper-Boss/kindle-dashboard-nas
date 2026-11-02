"""Network integration tests with local peers; no WhatsApp account is needed."""
import asyncio
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('whatsapp_proxy', Path(__file__).parents[1] / 'mtproto/whatsapp_proxy.py')
wa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wa)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_socks_remote_dns_and_bidirectional_bytes(self):
        received = {}

        async def socks_peer(reader, writer):
            try:
                await reader.readexactly(3)
                writer.write(b'\x05\x00'); await writer.drain()
                head = await reader.readexactly(5)
                received['target'] = (await reader.readexactly(head[4])).decode()
                received['port'] = int.from_bytes(await reader.readexactly(2), 'big')
                writer.write(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
                await writer.drain()
                received['payload'] = await reader.readexactly(5)
                writer.write(b'reply'); await writer.drain()
            finally:
                writer.close(); await writer.wait_closed()

        server = await asyncio.start_server(socks_peer, '127.0.0.1', 0)
        try:
            reader, writer = await wa.connect_socks('g.whatsapp.net', 5222, '127.0.0.1', server.sockets[0].getsockname()[1])
            writer.write(b'hello'); await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(5), 2), b'reply')
            writer.close(); await writer.wait_closed()
            self.assertEqual(received, {'target':'g.whatsapp.net','port':5222,'payload':b'hello'})
        finally:
            server.close(); await server.wait_closed()

    async def test_chat_prefix_and_clean_shutdown(self):
        observed = {}
        finished = asyncio.Event()

        async def upstream(reader, writer):
            observed['prefix'] = await reader.readline()
            observed['payload'] = await reader.readexactly(4)
            writer.write(b'pong'); await writer.drain()
            writer.close(); await writer.wait_closed()

        backend = await asyncio.start_server(upstream, '127.0.0.1', 0)

        async def dial(host, port):
            observed['target'] = (host, port)
            return await asyncio.open_connection('127.0.0.1', backend.sockets[0].getsockname()[1])

        async def handle(reader, writer):
            try:
                await wa.relay(reader, writer, 'chat')
            finally:
                finished.set()

        frontend = await asyncio.start_server(handle, '127.0.0.1', 0)
        try:
            with patch.object(wa, 'connect_socks', dial):
                reader, writer = await asyncio.open_connection('127.0.0.1', frontend.sockets[0].getsockname()[1])
                writer.write(b'ping'); await writer.drain(); writer.write_eof()
                self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 2), b'pong')
                writer.close(); await writer.wait_closed()
                await asyncio.wait_for(finished.wait(), 2)
            self.assertEqual(observed['target'], ('g.whatsapp.net',5222))
            self.assertTrue(observed['prefix'].startswith(b'PROXY TCP4 127.0.0.1 '))
            self.assertEqual(observed['payload'], b'ping')
            self.assertEqual(wa.STATE['active'], 0)
        finally:
            frontend.close(); backend.close()
            await frontend.wait_closed(); await backend.wait_closed()


if __name__ == '__main__':
    unittest.main()
