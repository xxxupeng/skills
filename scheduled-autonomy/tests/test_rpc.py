"""Real Unix/WebSocket transport fixture; no live Codex or model calls."""
import base64
import hashlib
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from test_scheduler import m


class SocketRPC(unittest.TestCase):
    def test_resume_subscription_captures_completion_before_start_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'server.sock')
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path); server.listen(1); server.settimeout(5)
            calls, errors = [], []
            def serve():
                def exact(c, n):
                    b = b''
                    while len(b) < n:
                        part = c.recv(n-len(b))
                        if not part: raise EOFError()
                        b += part
                    return b
                def send(c, value):
                    b = json.dumps(value).encode()
                    size = bytes([len(b)]) if len(b) < 126 else b'\x7e' + struct.pack('!H', len(b))
                    c.sendall(b'\x81' + size + b)
                try:
                    c, _ = server.accept()
                    with c:
                        c.settimeout(5); headers = b''
                        while not headers.endswith(b'\r\n\r\n'): headers += exact(c, 1)
                        key = next(line.split(b':', 1)[1].strip() for line in headers.split(b'\r\n') if line.lower().startswith(b'sec-websocket-key:'))
                        accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                        c.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                        while True:
                            h = exact(c, 2); n = h[1] & 127
                            if n == 126: n = struct.unpack('!H', exact(c, 2))[0]
                            if n == 127: n = struct.unpack('!Q', exact(c, 8))[0]
                            mask = exact(c, 4); body = exact(c, n)
                            if h[0] & 15 == 8: break
                            q = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(body)))
                            method = q['method']; calls.append((method, q.get('params')))
                            if 'id' not in q: continue
                            if method == 'initialize': result = {'userAgent': 'test'}
                            elif method == 'thread/resume': result = {'thread': {'id': 'original', 'cwd': '/tmp', 'status': {'type':'idle'}}}
                            elif method == 'turn/start':
                                send(c, {'method':'turn/completed','params': {'threadId':'original','turn':{'id':'turn-1','status':'completed'}}})
                                result = {'turn': {'id':'turn-1','status':'inProgress'}}
                            else: raise AssertionError(method)
                            send(c, {'id': q['id'], 'result': result})
                except Exception as e:
                    errors.append(e)
            t = threading.Thread(target=serve); t.start()
            peer = m.RPC(path)
            try:
                self.assertEqual(peer.resume('original')['id'], 'original')
                peer.start('original', 'one test')
                self.assertEqual(peer.completion('original', 'turn-1', 2)['status'], 'completed')
            finally:
                peer.close(); t.join(6); server.close()
            self.assertEqual(errors, [])
            self.assertEqual([c[0] for c in calls], ['initialize', 'initialized', 'thread/resume', 'turn/start'])
            self.assertEqual(calls[2][1], {'threadId':'original'})

if __name__ == '__main__': unittest.main()
