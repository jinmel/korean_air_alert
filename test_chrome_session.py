import base64
import hashlib
import json
import socket
import struct
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chrome_session

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def _read_exact(stream, size):
    data = b''
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise ConnectionError
        data += chunk
    return data


def _read_frame(stream):
    first, second = _read_exact(stream, 2)
    assert second & 0x80, 'client frames must be masked'
    size = second & 0x7F
    if size == 126:
        size = struct.unpack('!H', _read_exact(stream, 2))[0]
    elif size == 127:
        size = struct.unpack('!Q', _read_exact(stream, 8))[0]
    mask = _read_exact(stream, 4)
    payload = _read_exact(stream, size)
    return first & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _frame(opcode, payload, fin=True):
    size = len(payload)
    if size < 126:
        header = bytes([(0x80 if fin else 0) | opcode, size])
    else:
        header = bytes([(0x80 if fin else 0) | opcode, 126]) + struct.pack('!H', size)
    return header + payload


class FakeCdp:
    def __init__(self, storage=None, storage_error=False, page_cookies=None):
        self.storage = storage or []
        self.storage_error = storage_error
        self.page_cookies = page_cookies or []
        self.calls = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                port = self.server.server_address[1]
                if self.headers.get('Upgrade', '').lower() == 'websocket':
                    return self.websocket()
                if self.path == '/json/version':
                    body = {'webSocketDebuggerUrl': f'ws://127.0.0.1:{port}/devtools/browser/x'}
                elif self.path == '/json':
                    body = [{'type': 'service_worker'},
                            {'type': 'page', 'webSocketDebuggerUrl':
                             f'ws://127.0.0.1:{port}/devtools/page/y'}]
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def websocket(self):
                fake.origins.append(self.headers.get('Origin'))
                key = self.headers['Sec-WebSocket-Key']
                accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
                self.wfile.write((
                    'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                    f'Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n').encode())
                self.wfile.flush()
                target = 'page' if '/page/' in self.path else 'browser'
                try:
                    while True:
                        _opcode, payload = _read_frame(self.rfile)
                        message = json.loads(payload)
                        fake.calls.append((target, message['method'], message['params']))
                        self.wfile.write(_frame(0x1, json.dumps(
                            {'method': 'Some.event', 'params': {'pad': 'x' * 200}}).encode()))
                        self.wfile.write(_frame(0x9, b'ping'))
                        reply = fake.reply(target, message)
                        raw = json.dumps(reply).encode()
                        half = len(raw) // 2
                        self.wfile.write(_frame(0x1, raw[:half], fin=False))
                        self.wfile.write(_frame(0x0, raw[half:]))
                        self.wfile.flush()
                        opcode, pong = _read_frame(self.rfile)
                        assert (opcode, pong) == (0xA, b'ping')
                except (ConnectionError, OSError, ValueError):
                    pass
                self.close_connection = True

        self.origins = []
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, target, message):
        method = message['method']
        if target == 'browser' and method == 'Storage.getCookies':
            if self.storage_error:
                return {'id': message['id'], 'error': {'message': 'nope'}}
            return {'id': message['id'], 'result': {'cookies': self.storage}}
        if target == 'page' and method == 'Network.getAllCookies':
            return {'id': message['id'], 'result': {'cookies': self.page_cookies}}
        return {'id': message['id'], 'result': {}}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def cookie(name, value, domain):
    return {'name': name, 'value': value, 'domain': domain, 'path': '/'}


class ChromeSessionTests(unittest.TestCase):
    def fake(self, **kwargs):
        server = FakeCdp(**kwargs)
        self.addCleanup(server.close)
        return server

    def test_grabs_korean_air_t_and_ignores_other_domains(self):
        server = self.fake(storage=[
            cookie('T', 'aws-token', '.amazonaws.com'),
            cookie('RT', 'other', '.koreanair.com'),
            cookie('T', 'www-token', 'www.koreanair.com'),
            cookie('T', 'korean-air-token', '.koreanair.com')])
        self.assertEqual(chrome_session.grab_session_token(server.url), 'korean-air-token')
        self.assertEqual(server.calls, [('browser', 'Storage.getCookies', {})])
        self.assertEqual(server.origins, [None])

    def test_falls_back_to_page_network_cookies(self):
        server = self.fake(storage_error=True,
                           page_cookies=[cookie('T', 'page-token', 'www.koreanair.com')])
        self.assertEqual(chrome_session.grab_session_token(server.url), 'page-token')
        self.assertEqual([call[:2] for call in server.calls],
                         [('browser', 'Storage.getCookies'), ('page', 'Network.getAllCookies')])

    def test_missing_t_is_reported(self):
        server = self.fake(storage=[cookie('T', 'aws-token', '.amazonaws.com')])
        with self.assertRaises(RuntimeError) as caught:
            chrome_session.grab_session_token(server.url)
        self.assertIn('찾지 못했습니다', str(caught.exception))
        self.assertNotIn('aws-token', str(caught.exception))

    def test_unreachable_chrome_is_reported(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        with self.assertRaises(RuntimeError) as caught:
            chrome_session.grab_session_token(f'http://127.0.0.1:{port}', timeout=2)
        self.assertIn('연결할 수 없습니다', str(caught.exception))

    def test_push_session_token_sets_cookie(self):
        server = self.fake()
        chrome_session.push_session_token('rotated-token', server.url)
        self.assertEqual(server.calls, [('browser', 'Storage.setCookies', {'cookies': [{
            'name': 'T', 'value': 'rotated-token', 'domain': '.koreanair.com', 'path': '/',
            'secure': True, 'httpOnly': True}]})])


if __name__ == '__main__':
    unittest.main()
