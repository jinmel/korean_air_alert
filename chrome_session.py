"""Chrome 원격 디버깅(CDP)으로 본인 브라우저의 대한항공 세션 쿠키를 가져옵니다."""
import base64
import hashlib
import itertools
import json
import os
import socket
import struct
import urllib.error
import urllib.request
from urllib.parse import urlsplit

DEFAULT_CDP_URL = 'http://127.0.0.1:9222'
_WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


class _WebSocket:
    """Minimal RFC 6455 text-frame client for ws:// CDP endpoints."""

    def __init__(self, url, timeout):
        parts = urlsplit(url)
        if parts.scheme != 'ws' or not parts.hostname:
            raise RuntimeError('CDP WebSocket 주소가 올바르지 않습니다.')
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        self.sock = socket.create_connection((parts.hostname, parts.port or 80), timeout)
        self.buffer = b''
        key = base64.b64encode(os.urandom(16)).decode()
        host = parts.hostname + (f':{parts.port}' if parts.port else '')
        self.sock.sendall((
            f'GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n'
            f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
            'Sec-WebSocket-Version: 13\r\n\r\n').encode())
        header = self._read_until(b'\r\n\r\n').decode('latin-1')
        lines = header.split('\r\n')
        fields = {}
        for line in lines[1:]:
            name, _, value = line.partition(':')
            fields[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if ' 101 ' not in lines[0] + ' ' or fields.get('sec-websocket-accept') != expected:
            self.close()
            raise RuntimeError('CDP WebSocket 연결이 거부되었습니다.')

    def _read_until(self, marker):
        while marker not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError('CDP WebSocket 연결이 끊어졌습니다.')
            self.buffer += chunk
        data, _, self.buffer = self.buffer.partition(marker)
        return data

    def _read_exact(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError('CDP WebSocket 연결이 끊어졌습니다.')
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _send_frame(self, opcode, payload):
        header = bytes([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 1 << 16:
            header += bytes([0x80 | 126]) + struct.pack('!H', size)
        else:
            header += bytes([0x80 | 127]) + struct.pack('!Q', size)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send(self, text):
        self._send_frame(0x1, text.encode('utf-8'))

    def recv(self):
        message = b''
        while True:
            first, second = self._read_exact(2)
            opcode, fin = first & 0x0F, first & 0x80
            size = second & 0x7F
            if size == 126:
                size = struct.unpack('!H', self._read_exact(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', self._read_exact(8))[0]
            mask = self._read_exact(4) if second & 0x80 else None
            payload = self._read_exact(size)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise RuntimeError('CDP WebSocket 연결이 종료되었습니다.')
            message += payload
            if fin:
                return message.decode('utf-8')

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class CdpSession:
    def __init__(self, ws_url, timeout=10):
        self.ws = _WebSocket(ws_url, timeout)
        self.ids = itertools.count(1)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.ws.close()

    def call(self, method, **params):
        message_id = next(self.ids)
        self.ws.send(json.dumps({'id': message_id, 'method': method, 'params': params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get('id') != message_id:
                continue
            if 'error' in message:
                raise RuntimeError(f'CDP {method} 실패')
            return message.get('result', {})


def _get_json(cdp_url, path, timeout):
    try:
        with urllib.request.urlopen(cdp_url.rstrip('/') + path, timeout=timeout) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        raise RuntimeError(f'Chrome 원격 디버깅({cdp_url})에 연결할 수 없습니다.') from None


def _browser_ws(cdp_url, timeout):
    url = _get_json(cdp_url, '/json/version', timeout).get('webSocketDebuggerUrl')
    if not isinstance(url, str):
        raise RuntimeError(f'Chrome 원격 디버깅({cdp_url})에 연결할 수 없습니다.')
    return url


def _is_korean_air(domain):
    domain = (domain or '').lstrip('.').lower()
    return domain == 'koreanair.com' or domain.endswith('.koreanair.com')


def _read_cookies(cdp_url, timeout):
    try:
        with CdpSession(_browser_ws(cdp_url, timeout), timeout) as cdp:
            return cdp.call('Storage.getCookies').get('cookies', [])
    except (OSError, RuntimeError) as exc:
        if '연결할 수 없습니다' in str(exc):
            raise
    targets = _get_json(cdp_url, '/json', timeout)
    page = next((target for target in targets if isinstance(target, dict)
                 and target.get('type') == 'page'
                 and isinstance(target.get('webSocketDebuggerUrl'), str)), None)
    if page is None:
        raise RuntimeError('Chrome에서 쿠키를 읽을 페이지를 찾지 못했습니다.')
    try:
        with CdpSession(page['webSocketDebuggerUrl'], timeout) as cdp:
            return cdp.call('Network.getAllCookies').get('cookies', [])
    except OSError:
        raise RuntimeError(f'Chrome 원격 디버깅({cdp_url})에 연결할 수 없습니다.') from None


def grab_session_token(cdp_url=DEFAULT_CDP_URL, timeout=10):
    cookies = [cookie for cookie in _read_cookies(cdp_url, timeout)
               if isinstance(cookie, dict) and cookie.get('name') == 'T'
               and _is_korean_air(cookie.get('domain'))
               and isinstance(cookie.get('value'), str) and cookie['value']]
    if not cookies:
        raise RuntimeError('Chrome에서 대한항공 로그인 세션(T)을 찾지 못했습니다: '
                           '그 Chrome에서 koreanair.com에 로그인하세요.')
    preferred = [cookie for cookie in cookies if cookie.get('domain') == '.koreanair.com']
    return (preferred or cookies)[0]['value']


def push_session_token(token, cdp_url=DEFAULT_CDP_URL, timeout=10):
    """Best-effort: keep the browser's T in sync with the token rotated by cron."""
    cookie = {'name': 'T', 'value': token, 'domain': '.koreanair.com', 'path': '/',
              'secure': True, 'httpOnly': True}
    with CdpSession(_browser_ws(cdp_url, timeout), timeout) as cdp:
        cdp.call('Storage.setCookies', cookies=[cookie])
