import concurrent.futures
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import socket
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from diaodu import relay_http
from diaodu.identity import Identity, digest
from diaodu.library import Library
from diaodu.relay_client import RelayClient, RelayError, environment, load_state, main
from diaodu.server import make_handler


class RelayHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = Library(self.root, '/task')
        self.identity = Identity(self.root / 'identity.sqlite3')
        self.identities = {role: self.identity.connect(role, '测试Agent')
                           for role in ('owner', 'mentor', 'worker', 'outsider')}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(
            None, self.library, self.identity, public_origin='https://www.chuhaijian.com'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.clients = {role: RelayClient(self.base + '/v1', value['token'])
                        for role, value in self.identities.items()}
        self.created = self.clients['owner'].create('跨系统协作', '指导端检查执行端OS后指导工作')
        self.sid = self.created['session']['id']

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def req(self, method, path, body=None, role=None, headers=None):
        h = {'Content-Type': 'application/json'}
        if role:
            h['Authorization'] = 'Bearer ' + self.identities[role]['token']
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urlopen(request, timeout=8) as response:
                raw = response.read()
                return response.status, json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else raw
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def join(self):
        self.clients['mentor'].join(self.created['topic_code'], 'mentor')
        self.clients['worker'].join(self.created['topic_code'], 'worker')

    def test_client_abandoned_detects_peer_close(self):
        left, right = socket.socketpair()
        try:
            handler = type("Handler", (), {"connection": left})()
            self.assertFalse(relay_http.client_abandoned(handler))
            right.close()
            right = None
            self.assertTrue(relay_http.client_abandoned(handler))
        finally:
            left.close()
            if right is not None:
                right.close()

    def test_same_identity_long_poll_returns_waiter_busy(self):
        self.join()
        status, caught_up = self.req("GET", f"/v1/relay/sessions/{self.sid}/events?after=0&wait=0", role="worker")
        self.assertEqual(status, 200, caught_up)
        path = f"/v1/relay/sessions/{self.sid}/events?after={caught_up['next_cursor']}&wait=20"
        self.library.relay.MAX_WAITERS = 1
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            first = pool.submit(self.req, "GET", path, None, "worker")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not self.library.relay._waiters:
                time.sleep(0.01)
            self.assertTrue(self.library.relay._waiters, "long poll did not claim a waiter")
            status, body = self.req("GET", path, role="worker")
            self.assertEqual(status, 429)
            self.assertEqual(body.get("code"), "waiter_busy")
            self.assertIn("该身份已有实时等待连接", body.get("error", ""))
            capped_status, capped = self.req("GET", path, role="mentor")
            self.assertEqual(capped_status, 429)
            self.assertEqual(capped.get("code"), "rate_limited")
            self.assertNotIn("waiter_busy", json.dumps(capped))
            self.clients["owner"].send(self.sid, "note", "唤醒长轮询", "wake-waiter")
            woken = first.result(timeout=3)
            self.assertEqual(woken[0], 200)

    def test_disconnected_long_poll_releases_the_waiter(self):
        self.join()
        cursor = self.clients['worker'].get(self.sid)['session']['last_seq']
        port = self.server.server_address[1]
        sock = socket.create_connection(('127.0.0.1', port))
        try:
            token = self.identities['worker']['token']
            sock.sendall((
                f"GET /v1/relay/sessions/{self.sid}/events?after={cursor}&wait=20 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {token}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self.library.relay._condition:
                    if self.library.relay._waiters:
                        break
                time.sleep(0.01)
            else:
                self.fail("long poll did not register")
            sock.close()
            sock = None
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with self.library.relay._condition:
                    if not self.library.relay._waiters:
                        break
                time.sleep(0.02)
            else:
                self.fail("waiter still held after the client disconnected")
        finally:
            if sock is not None:
                sock.close()
        status, _ = self.req('GET', f'/v1/relay/sessions/{self.sid}/events?after={cursor}&wait=0', role='worker')
        self.assertEqual(status, 200)
        self.assertEqual(self.library.relay._waiters, set())

    def test_anonymous_routes_private_history_and_required_os(self):
        for route in ('/v1/relay/sessions', '/v1/relay/sessions/' + self.sid):
            self.assertEqual(self.req('GET', route)[0], 401)
        for route in ('/relay/', '/relay/relay.js', '/relay/relay.css', '/v1/relay/guide.md', '/v1/relay/client.py'):
            self.assertEqual(self.req('GET', route)[0], 200, route)
        self.assertEqual(self.req('POST', '/v1/relay/join', {'code': self.created['topic_code'], 'role': 'worker'}, 'worker')[0], 400)
        self.join()
        session = self.clients['mentor'].get(self.sid)['session']
        worker = next(peer for peer in session['peers'] if peer['role'] == 'worker')
        self.assertEqual(worker['environment'], environment())
        self.assertEqual(self.req('GET', '/v1/relay/sessions/' + self.sid, role='outsider')[0], 404)
        self.assertEqual(self.clients['outsider'].list(), {'sessions': []})
        public = self.req('GET', '/v1/tasks')[1]
        self.assertNotIn('跨系统协作', json.dumps(public, ensure_ascii=False))

    def test_topic_code_joins_from_independent_agents(self):
        code=self.created['topic_code']
        status,mentor=self.req('POST','/v1/relay/join',{'code':code,'role':'mentor'},'mentor')
        self.assertEqual(status,200);self.assertEqual(mentor['session']['role'],'mentor')
        status,worker=self.req('POST','/v1/relay/join',{'code':code,'role':'worker','environment':{
            'os_name':'Linux','os_version':'test','architecture':'x86_64'}},'worker')
        self.assertEqual(status,200);self.assertEqual(worker['session']['role'],'worker')
        status,_=self.req('POST','/v1/relay/join',{'code':code,'role':'mentor'},'outsider')
        self.assertEqual(status,409)
        status,new_code=self.req('POST','/v1/relay/sessions/'+self.sid+'/topic-code',{},'owner')
        self.assertEqual(status,200);self.assertNotEqual(new_code['code'],code)

    def test_client_joins_from_topic_code_without_mcp_or_prior_identity(self):
        code_file = self.root / 'topic-code'
        code_file.write_text(self.created['topic_code'])
        code_file.chmod(0o600)
        state_file = self.root / 'agent-state.json'
        output = io.StringIO()
        with patch('sys.argv', ['relay_client.py', '--url', self.base + '/v1',
                                '--state-file', str(state_file), 'join', '--role', 'worker',
                                '--alias', 'Worker Agent', '--code-file', str(code_file)]):
            with redirect_stdout(output):
                main()
        state = load_state(state_file)
        token = state[self.base + '/v1']['token']
        self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(token, output.getvalue())
        actor = self.identity.agent(token)
        self.assertEqual(actor['agent_name'], 'Worker Agent')
        joined = RelayClient(self.base + '/v1', token).get(self.sid)['session']
        self.assertEqual(joined['role'], 'worker')
        worker = next(peer for peer in joined['peers'] if peer['role'] == 'worker')
        self.assertEqual(worker['environment'], environment())

    def test_real_http_long_poll_roundtrip_dedup_and_pause(self):
        self.join()
        cursor = self.clients['worker'].events(self.sid, wait=0)['next_cursor']
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(self.clients['worker'].events, self.sid, cursor, 5)
            time.sleep(.1)
            start = time.monotonic()
            sent = self.clients['mentor'].send(self.sid, 'instruction', '检查系统后运行测试', 'instruction-1')
            batch = waiting.result(timeout=3)
            self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(batch['events'][0]['message']['text'], '检查系统后运行测试')
        duplicate = self.clients['mentor'].send(self.sid, 'instruction', '检查系统后运行测试', 'instruction-1')
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['event']['seq'], sent['event']['seq'])
        self.clients['owner'].state(self.sid, 'paused')
        with self.assertRaises(RelayError) as paused:
            self.clients['mentor'].send(self.sid, 'instruction', '不能启动新操作', 'instruction-2')
        self.assertEqual(paused.exception.status, 409)
        result = self.clients['worker'].send(self.sid, 'result', '检查结果', 'result-1', sent['event']['seq'])
        self.assertEqual(result['event']['sender']['role'], 'worker')
        self.assertEqual(self.clients['worker'].ack(self.sid, result['event']['seq'])['acknowledged'], result['event']['seq'])

    def test_cookie_csrf_origin_and_owner_controls(self):
        self.join()
        path = '/v1/relay/sessions/' + self.sid + '/state'
        token = self.identities['owner']['token']
        cookie = {'Cookie': 'diaodu_session=' + token}
        self.assertEqual(self.req('POST', path, {'status': 'paused'}, headers=cookie)[0], 403)
        cookie['X-CSRF-Token'] = digest('csrf:' + token)
        self.assertEqual(self.req('POST', path, {'status': 'paused'}, headers=cookie)[0], 200)
        self.assertEqual(self.req('POST', path, {'status': 'active'}, 'mentor')[0], 403)
        self.assertEqual(self.req('POST', path, {'status': 'active'}, 'owner', {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.req('POST', '/v1/relay/sessions/' + self.sid + '/revoke', {'role': 'worker'}, 'owner')[0], 200)
        self.assertEqual(self.req('GET', '/v1/relay/sessions/' + self.sid, role='worker')[0], 404)

    def test_bearer_revocation_while_waiting_does_not_deliver_private_payload(self):
        self.join()
        cursor = self.clients['worker'].events(self.sid, wait=0)['next_cursor']
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(self.clients['worker'].events, self.sid, cursor, 5)
            time.sleep(.1)
            worker = self.identities['worker']
            self.identity.revoke(worker['user_id'], worker['agent_id'])
            self.clients['mentor'].send(self.sid, 'note', '撤销后不得交付的消息', 'after-revoke')
            with self.assertRaises(RelayError) as revoked:
                waiting.result(timeout=3)
            self.assertEqual(revoked.exception.status, 401)

    def test_mcp_uses_same_private_relay_and_requires_current_credentials(self):
        def call(name, args, role=None, sid=None):
            body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'name': 'diaodu_' + name, 'arguments': args}}
            return self.req('POST', '/v1/mcp', body, role, {'Mcp-Session-Id': sid} if sid else None)
        status, body = call('relay_join', {'code': self.created['topic_code'], 'role': 'mentor'}, 'mentor')
        self.assertEqual(status,200); self.assertFalse(body['result']['isError'])
        status, body = call('relay_join', {'code': self.created['topic_code'], 'role': 'worker', 'environment': {
            'os_name': 'Linux', 'os_version': 'test', 'architecture': 'x86_64'}}, 'worker')
        self.assertEqual(status,200); self.assertFalse(body['result']['isError'])
        status, body = call('relay_read', {'session': self.sid, 'wait': 0}, 'mentor')
        self.assertEqual(status, 200)
        self.assertFalse(body['result']['isError'])
        self.assertEqual(json.loads(body['result']['content'][0]['text'])['session']['role'], 'mentor')
        status, body = call('relay_send', {'session': self.sid, 'kind': 'instruction', 'text': 'MCP指导', 'request_id': 'mcp-1'}, 'mentor')
        self.assertEqual(status, 200)
        self.assertFalse(body['result']['isError'])
        self.assertTrue(any(event.get('message', {}).get('text') == 'MCP指导'
                            for event in self.clients['worker'].events(self.sid, wait=0)['events']))

    def mcp_request(self, payload, sid=None, role=None):
        headers = {'Content-Type': 'application/json'}
        if role:
            headers['Authorization'] = 'Bearer ' + self.identities[role]['token']
        if sid:
            headers['Mcp-Session-Id'] = sid
        request = Request(self.base + '/v1/mcp', data=json.dumps(payload).encode(),
                          headers=headers, method='POST')
        try:
            with urlopen(request, timeout=8) as response:
                return response.status, json.load(response), response.headers.get('Mcp-Session-Id')
        except HTTPError as error:
            with error:
                return error.code, json.load(error), None

    def mcp_initialize(self):
        status, body, sid = self.mcp_request(
            {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}})
        self.assertEqual(status, 200)
        self.assertTrue(sid)
        return sid

    def mcp_call(self, name, args, sid=None, role=None):
        status, body, _ = self.mcp_request(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
             'params': {'name': 'diaodu_' + name, 'arguments': args}}, sid, role)
        return status, body

    @staticmethod
    def mcp_payload(body):
        return json.loads(body['result']['content'][0]['text'])

    def test_mcp_session_identity_works_without_bearer(self):
        sid = self.mcp_initialize()
        status, body = self.mcp_call('connect', {'alias': '无Bearer端', 'agent_type': 'Codex'}, sid)
        self.assertEqual(status, 200)
        self.assertFalse(body['result']['isError'])
        connected = self.mcp_payload(body)
        self.assertTrue(connected['agent_id'])
        self.assertTrue(connected['token'].startswith('da_'))
        status, body = self.mcp_call('relay_create', {'title': 'MCP 无 Bearer', 'goal': '一条命令接入'}, sid)
        self.assertFalse(body['result']['isError'])
        created = self.mcp_payload(body)
        created_sid = created['session']['id']
        status, body = self.mcp_call('relay_list', {}, sid)
        self.assertEqual([session['id'] for session in self.mcp_payload(body)['sessions']], [created_sid])
        sid2 = self.mcp_initialize()
        status, body = self.mcp_call('connect', {'alias': '无Bearer执行端', 'agent_type': 'Codex'}, sid2)
        member = self.mcp_payload(body)
        status, body = self.mcp_call('relay_join', {
            'code': created['topic_code'], 'role': 'worker',
            'environment': {'os_name': 'macOS', 'os_version': 'test', 'architecture': 'arm64'}}, sid2)
        self.assertFalse(body['result']['isError'])
        status, body = self.mcp_call('relay_read', {'session': created_sid, 'wait': 0}, sid2)
        self.assertFalse(body['result']['isError'])
        status, body = self.mcp_call('relay_send', {
            'session': created_sid, 'kind': 'progress', 'text': '已接入', 'request_id': 'mcp-session-1'}, sid2)
        self.assertFalse(body['result']['isError'])
        seq = self.mcp_payload(body)['event']['seq']
        status, body = self.mcp_call('relay_ack', {'session': created_sid, 'seq': seq}, sid2)
        self.assertEqual(self.mcp_payload(body)['acknowledged'], seq)
        sid3 = self.mcp_initialize()
        status, body = self.mcp_call('relay_list', {}, sid3)
        self.assertTrue(body['result']['isError'])
        self.assertEqual(self.mcp_payload(body)['code'], 401)
        self.identity.revoke(member['user_id'], member['agent_id'])
        status, body = self.mcp_call('relay_list', {}, sid2)
        self.assertTrue(body['result']['isError'])
        self.assertEqual(self.mcp_payload(body)['code'], 401)

    def test_mcp_explicit_bearer_wins_over_session_identity(self):
        sid = self.mcp_initialize()
        status, body = self.mcp_call('connect', {'alias': '会话身份', 'agent_type': 'Codex'}, sid)
        self.assertFalse(body['result']['isError'])
        status, body = self.mcp_call('relay_create', {'title': 'Bearer 优先', 'goal': '身份归属'}, sid, role='mentor')
        self.assertFalse(body['result']['isError'])
        created = self.mcp_payload(body)
        self.assertTrue(created['session']['owner'])
        status, body = self.mcp_call('relay_list', {}, sid)
        listed = self.mcp_payload(body)
        self.assertNotIn(created['session']['id'], [session['id'] for session in listed['sessions']])
        status, body = self.mcp_call('relay_list', {}, role='mentor')
        listed = self.mcp_payload(body)
        self.assertIn(created['session']['id'], [session['id'] for session in listed['sessions']])

    def test_input_limits_and_identity_spoofing(self):
        self.join()
        prefix = '/v1/relay/sessions/' + self.sid
        self.assertEqual(self.req('GET', prefix + '/events?after=0&after=1', role='worker')[0], 400)
        self.assertEqual(self.req('GET', prefix + '/events?wait=21', role='worker')[0], 400)
        self.assertEqual(self.req('POST', prefix + '/messages', {'kind': 'instruction', 'text': 'x', 'request_id': 'x', 'role': 'mentor'}, 'worker')[0], 400)
        self.assertEqual(self.req('POST', prefix + '/messages', {'kind': 'note', 'text': 'x' * 70000, 'request_id': 'large'}, 'mentor')[0], 400)
        self.assertEqual(self.req('POST', prefix + '/heartbeat', {'environment': environment()}, 'mentor')[0], 403)

    def raw_post(self, path, declared, payload, shutdown=True, timeout=5):
        """Send a request whose Content-Length lies, without urllib's help."""
        host, port = self.server.server_address[:2]
        request = ('POST ' + path + ' HTTP/1.1\r\n'
                   'Host: 127.0.0.1\r\n'
                   'Authorization: Bearer ' + self.identities['mentor']['token'] + '\r\n'
                   'Content-Type: application/json\r\n'
                   'Content-Length: ' + str(declared) + '\r\n'
                   'Connection: keep-alive\r\n\r\n').encode()
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            sock.sendall(request + payload)
            if shutdown:
                sock.shutdown(socket.SHUT_WR)
            data = b''
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            except ConnectionResetError:
                pass
            return data
        finally:
            sock.close()

    def test_oversized_body_reject_returns_parseable_json(self):
        path = '/v1/relay/sessions/' + self.sid + '/messages'
        raw = self.raw_post(path, 70000, b'{"kind":"note","text":"x')
        head, _, body = raw.partition(b'\r\n\r\n')
        self.assertIn(b' 400 ', head.split(b'\r\n')[0])
        self.assertIn(b'Connection: close', head)
        self.assertEqual(json.loads(body)['error'], '请求过大')

    def test_body_beyond_drain_cap_closes_without_reading(self):
        path = '/v1/relay/sessions/' + self.sid + '/messages'
        started = time.monotonic()
        raw = self.raw_post(path, 32 * 1024 * 1024, b'{"kind":"note"', shutdown=False)
        elapsed = time.monotonic() - started
        # 32MB > 16MB cap: the server must not wait for the rest of the body.
        self.assertLess(elapsed, 2)
        self.assertTrue(raw == b'' or b' 400 ' in raw.split(b'\r\n')[0],
                        raw[:80])


if __name__ == '__main__':
    unittest.main()
