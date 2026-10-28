"""Hub-authenticated reverse proxy in front of the local design MCP."""
import json
import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import test_identity as fixtures
from diaodu import design_mcp


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, fmt, *args):
        return

    def _record(self):
        length = int(self.headers.get('Content-Length', '0') or 0)
        body = self.rfile.read(length) if length else b''
        item = {
            'method': self.command,
            'path': self.path,
            'authorization': self.headers.get('Authorization'),
            'headers': {key.lower(): value for key, value in self.headers.items()},
            'body': body,
        }
        with self.server.lock:
            self.server.seen.append(item)
        return item

    def do_GET(self):
        self._record()
        self._json(200, {'ok': True, 'via': 'get'})

    def do_DELETE(self):
        self._record()
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        item = self._record()
        try:
            payload = json.loads(item['body'].decode() or '{}')
        except json.JSONDecodeError:
            payload = {}
        method = payload.get('method')
        if method == 'stream':
            self._sse()
            return
        if method == 'bad':
            data = b'{"error":"bad-tool"}'
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if method == 'initialize':
            result = {
                'protocolVersion': '2025-11-25',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'design', 'version': '1'},
            }
            extra = {'Mcp-Session-Id': 'design-session-1'}
        elif method == 'tools/list':
            result = {'tools': [{
                'name': 'stitch_render',
                'description': 'render a design',
                'inputSchema': {'type': 'object'},
            }]}
            extra = {}
        else:
            result = {}
            extra = {}
        self._json(200, {'jsonrpc': '2.0', 'id': payload.get('id'), 'result': result}, extra)

    def _json(self, code, payload, extra=None):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _sse(self):
        self.close_connection = True
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        self._chunk(b'data: event-one\n\n')
        self.server.gate.wait(3)
        self._chunk(b'data: event-two\n\n')
        self.wfile.write(b'0\r\n\r\n')
        self.wfile.flush()

    def _chunk(self, data):
        self.wfile.write(b'%X\r\n' % len(data))
        self.wfile.write(data)
        self.wfile.write(b'\r\n')
        self.wfile.flush()


class UpstreamServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), UpstreamHandler)
        self.seen = []
        self.lock = threading.Lock()
        self.gate = threading.Event()


class DesignMCPProxyTest(unittest.TestCase):
    req = fixtures.IdentityAPI.req

    def setUp(self):
        self.upstream = UpstreamServer()
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        port = self.upstream.server_address[1]
        self._saved = {
            'DESIGN_MCP_UPSTREAM': os.environ.get('DESIGN_MCP_UPSTREAM'),
            'DESIGN_MCP_TOKEN': os.environ.get('DESIGN_MCP_TOKEN'),
            'DESIGN_MCP_TIMEOUT': os.environ.get('DESIGN_MCP_TIMEOUT'),
        }
        os.environ['DESIGN_MCP_UPSTREAM'] = f'http://127.0.0.1:{port}/mcp/design/'
        os.environ['DESIGN_MCP_TOKEN'] = 'design-secret-token'
        os.environ['DESIGN_MCP_TIMEOUT'] = '10'
        fixtures.IdentityAPI.setUp(self)

    def tearDown(self):
        try:
            if hasattr(self, 'server'):
                fixtures.IdentityAPI.tearDown(self)
        finally:
            self.upstream.shutdown()
            self.upstream.server_close()
            self.upstream_thread.join(timeout=2)
            for key, value in self._saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def raw(self, method, path, body=None, headers=None, timeout=5):
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = Request(self.base + path, data=data, headers=headers or {}, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.status, response.headers, response.read()
        except HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def rpc(self, method, id=1, params=None):
        return {'jsonrpc': '2.0', 'id': id, 'method': method, 'params': params or {}}

    def bearer(self, agent=0):
        return {
            'Authorization': 'Bearer ' + self.agents[agent]['token'],
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
        }

    def test_unauthenticated_requests_are_401(self):
        before = len(self.upstream.seen)
        for method in ('POST', 'GET', 'DELETE'):
            body = self.rpc('initialize') if method == 'POST' else None
            status, _, payload = self.raw(method, '/v1/mcp/design/', body)
            self.assertEqual(status, 401, method)
            self.assertEqual(json.loads(payload)['code'], 'unauthenticated')
        self.assertEqual(len(self.upstream.seen), before)

    def test_wrong_hub_auth_is_401(self):
        before = len(self.upstream.seen)
        status, _, payload = self.raw('POST', '/v1/mcp/design/', self.rpc('initialize'), {
            'Authorization': 'Bearer not-a-hub-token',
            'Content-Type': 'application/json',
        })
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(payload)['code'], 'unauthenticated')
        status, _, _ = self.raw('POST', '/v1/mcp/design', self.rpc('initialize'), {
            'Authorization': 'Token abc',
            'Content-Type': 'application/json',
        })
        self.assertEqual(status, 401)
        status, _, _ = self.raw('POST', '/v1/mcp/design/', self.rpc('initialize'), {
            'Cookie': 'diaodu_session=not-a-session',
            'X-CSRF-Token': 'nope',
            'Content-Type': 'application/json',
        })
        self.assertEqual(status, 401)
        self.assertEqual(len(self.upstream.seen), before)

    def test_bearer_can_initialize_and_list_tools(self):
        status, headers, body = self.raw('POST', '/v1/mcp/design', self.rpc('initialize', params={'marker': 'init-body'}), self.bearer())
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload['result']['serverInfo']['name'], 'design')
        self.assertEqual(payload['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(headers.get('Mcp-Session-Id'), 'design-session-1')
        self.assertEqual(headers.get('X-Accel-Buffering'), 'no')
        self.assertNotIn(b'design-secret-token', body)
        first = self.upstream.seen[-1]
        self.assertEqual(first['path'], '/mcp/design/')
        self.assertEqual(first['authorization'], 'Bearer design-secret-token')
        self.assertIn('init-body', first['body'].decode())
        self.assertNotIn(self.agents[0]['token'], json.dumps(first['headers']))
        self.assertNotIn('cookie', first['headers'])

        listed = dict(self.bearer(), **{'Mcp-Session-Id': 'design-session-1'})
        status, _, body = self.raw('POST', '/v1/mcp/design/?trace=1', self.rpc('tools/list', id=7), listed)
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload['id'], 7)
        self.assertEqual(payload['result']['tools'][0]['name'], 'stitch_render')
        self.assertNotIn(b'design-secret-token', body)
        second = self.upstream.seen[-1]
        self.assertEqual(second['path'], '/mcp/design/?trace=1')
        self.assertEqual(second['authorization'], 'Bearer design-secret-token')
        self.assertEqual(second['headers'].get('mcp-session-id'), 'design-session-1')
        self.assertNotIn(self.agents[0]['token'], second['authorization'])

    def test_session_cookie_is_accepted_and_not_forwarded(self):
        status, _, body = self.raw('POST', '/v1/mcp/design/', self.rpc('initialize'), {
            'Cookie': self.cookies[0],
            'X-CSRF-Token': self.csrf[0],
            'Content-Type': 'application/json',
        })
        self.assertEqual(status, 200, body)
        seen = self.upstream.seen[-1]
        self.assertEqual(seen['authorization'], 'Bearer design-secret-token')
        self.assertNotIn('cookie', seen['headers'])
        self.assertNotIn('x-csrf-token', seen['headers'])
        self.assertNotIn(self.csrf[0], json.dumps(seen['headers']))
        before = len(self.upstream.seen)
        status, _, payload = self.raw('POST', '/v1/mcp/design/', self.rpc('initialize'), {
            'Cookie': self.cookies[0],
            'Content-Type': 'application/json',
        })
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload)['code'], 'forbidden')
        self.assertEqual(len(self.upstream.seen), before)

    def test_get_and_delete_are_forwarded_for_an_authenticated_caller(self):
        headers = self.bearer()
        status, _, body = self.raw('GET', '/v1/mcp/design/', headers=headers)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['via'], 'get')
        self.assertEqual(self.upstream.seen[-1]['method'], 'GET')
        self.assertEqual(self.upstream.seen[-1]['authorization'], 'Bearer design-secret-token')
        status, response_headers, body = self.raw('DELETE', '/v1/mcp/design/', headers=headers)
        self.assertEqual(status, 204, body)
        self.assertEqual(body, b'')
        self.assertEqual(self.upstream.seen[-1]['method'], 'DELETE')
        self.assertNotIn('design-secret-token', response_headers.get('Set-Cookie', '') or '')

    def test_streaming_response_is_flushed_and_request_body_is_forwarded(self):
        self.upstream.gate.clear()
        body = self.rpc('stream', id=3, params={'marker': 'body-marker-stream'})
        request = Request(
            self.base + '/v1/mcp/design/',
            data=json.dumps(body).encode(),
            headers={
                'Authorization': 'Bearer ' + self.agents[0]['token'],
                'Content-Type': 'application/json',
                'Accept': 'text/event-stream',
            },
            method='POST',
        )
        with urlopen(request, timeout=8) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('text/event-stream', response.headers.get('Content-Type', ''))
            self.assertEqual(response.headers.get('X-Accel-Buffering'), 'no')
            self.assertIn('no-transform', response.headers.get('Cache-Control', ''))
            got = b''
            while b'event-one' not in got:
                part = response.read1(4096)
                self.assertTrue(part, 'response was buffered until the upstream stream ended')
                got += part
            self.assertNotIn(b'event-two', got)
            self.upstream.gate.set()
            rest = got
            while b'event-two' not in rest:
                part = response.read1(4096)
                self.assertTrue(part)
                rest += part
        self.assertNotIn(b'design-secret-token', rest)
        seen = self.upstream.seen[-1]
        self.assertEqual(seen['authorization'], 'Bearer design-secret-token')
        self.assertIn('body-marker-stream', seen['body'].decode())
        self.assertNotIn(self.agents[0]['token'], json.dumps(seen['headers']))

    def test_upstream_error_body_is_forwarded_unchanged(self):
        status, _, body = self.raw('POST', '/v1/mcp/design/', self.rpc('bad'), self.bearer())
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {'error': 'bad-tool'})

    def test_health_is_open_and_does_not_call_upstream(self):
        before = len(self.upstream.seen)
        status, body = self.req('GET', '/v1/mcp/design/health')
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {'ok', 'service', 'configured'})
        self.assertTrue(body['ok'])
        self.assertTrue(body['configured'])
        self.assertEqual(body['service'], 'design-mcp')
        self.assertNotIn('design-secret-token', json.dumps(body))
        self.assertEqual(len(self.upstream.seen), before)
        os.environ.pop('DESIGN_MCP_TOKEN')
        status, body = self.req('GET', '/v1/mcp/design/health')
        self.assertEqual(body['configured'], False)
        status, error = self.req('POST', '/v1/mcp/design/', self.rpc('initialize'), agent=0)
        self.assertEqual(status, 503)
        self.assertEqual(error['error'], '设计服务未配置')
        self.assertNotIn('design-secret-token', json.dumps(error))
        self.assertEqual(len(self.upstream.seen), before)

    def test_oversized_body_does_not_reach_upstream(self):
        before = len(self.upstream.seen)
        sock = socket.create_connection(('127.0.0.1', self.server.server_address[1]))
        try:
            sock.sendall((
                'POST /v1/mcp/design/ HTTP/1.1\r\n'
                'Host: 127.0.0.1\r\n'
                'Authorization: Bearer ' + self.agents[0]['token'] + '\r\n'
                'Content-Length: ' + str(design_mcp.MAX_BODY + 1) + '\r\n'
                'Connection: close\r\n'
                '\r\n'
            ).encode())
            data = b''
            while True:
                part = sock.recv(4096)
                if not part:
                    break
                data += part
        finally:
            sock.close()
        self.assertIn(b' 400 ', data.split(b'\r\n', 1)[0])
        self.assertEqual(len(self.upstream.seen), before)

    def test_token_prefix_is_not_doubled_and_default_upstream(self):
        os.environ['DESIGN_MCP_TOKEN'] = 'Bearer design-secret-token'
        status, _, body = self.raw('POST', '/v1/mcp/design/', self.rpc('initialize'), self.bearer())
        self.assertEqual(status, 200, body)
        self.assertEqual(self.upstream.seen[-1]['authorization'], 'Bearer design-secret-token')
        saved = os.environ.pop('DESIGN_MCP_UPSTREAM')
        try:
            self.assertEqual(design_mcp.upstream_base(), 'http://127.0.0.1:18790/mcp/design/')
        finally:
            os.environ['DESIGN_MCP_UPSTREAM'] = saved

    def test_existing_task_mcp_and_public_pages_are_unchanged(self):
        before = len(self.upstream.seen)
        status, body = self.req('GET', '/v1/tasks')
        self.assertEqual(status, 200)
        self.assertIn('tasks', body)
        status, body = self.req('GET', '/v1/mcp')
        self.assertEqual(status, 405)
        self.assertEqual(body['transport'], 'Streamable HTTP JSON')
        status, body = self.req('POST', '/v1/mcp', {
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}},
        })
        self.assertEqual(status, 200)
        self.assertEqual(body['result']['serverInfo']['name'], 'diaodu')
        status, _, page = self.raw('GET', '/conversations/')
        self.assertEqual(status, 200)
        self.assertIn(b'html', page.lower())
        status, spec = self.req('GET', '/v1/openapi.json')
        self.assertEqual(status, 200)
        self.assertIn('/mcp/design', spec['paths'])
        self.assertEqual(len(self.upstream.seen), before)
