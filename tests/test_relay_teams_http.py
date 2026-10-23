import base64
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from diaodu.identity import Identity
from diaodu.library import Library
from diaodu.server import make_handler

ENVIRONMENT = {"os_name": "macOS", "os_version": "test", "architecture": "arm64",
               "shells": ["bash"], "python_version": "3.11.15"}


class TeamsHTTP(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.library = Library(root, '/task')
        self.identity = Identity(root / 'identity.sqlite3', '')
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(
            None, self.library, self.identity, public_origin='https://www.chuhaijian.com'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.actors = {name: self.identity.connect(name, '测试Agent')
                       for name in ('owner', 'mentor', 'w1', 'w2', 'w3', 'outsider')}
        status, created = self.req('POST', '/v1/relay/teams', {'title': '演示团队', 'goal': '并行干活'}, 'owner')
        self.assertEqual(status, 201)
        self.sid = created['session']['id']
        self.mentor_code = self.invite('mentor')['code']
        self.req('POST', '/v1/relay/teams/join',
                 {'code': self.mentor_code, 'role': 'mentor'}, 'mentor')
        self.workers = {}
        for name in ('w1', 'w2', 'w3'):
            invite = self.invite('worker')
            status, joined = self.req('POST', '/v1/relay/teams/join',
                                      {'code': invite['code'], 'environment': dict(ENVIRONMENT)}, name)
            self.assertEqual(status, 200)
            self.workers[name] = joined['session']['seat']

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temporary.cleanup()

    def req(self, method, path, body=None, role=None, headers=None):
        head = {'Content-Type': 'application/json'}
        if role:
            head['Authorization'] = 'Bearer ' + self.actors[role]['token']
        head.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urlopen(Request(self.base + path, data=data, headers=head, method=method), timeout=30) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def invite(self, role, seat=None, note=''):
        body = {'role': role, 'note': note}
        if seat is not None:
            body['seat'] = seat
        status, result = self.req('POST', f'/v1/relay/teams/{self.sid}/invites', body, 'owner')
        self.assertEqual(status, 201)
        return result['invite']

    def send(self, role, kind, text, request_id, **extra):
        return self.req('POST', f'/v1/relay/teams/{self.sid}/messages',
                        {'kind': kind, 'text': text, 'request_id': request_id, **extra}, role)

    def events(self, role, after=0):
        status, data = self.req('GET', f'/v1/relay/teams/{self.sid}/events?after={after}&wait=0', role=role)
        self.assertEqual(status, 200)
        return data['events']

    def upload(self, role, name, content):
        request = Request(self.base + f'/v1/relay/teams/{self.sid}/files?name=' + quote(name),
                          data=content, headers={'Content-Type': 'application/octet-stream',
                                                 'Authorization': 'Bearer ' + self.actors[role]['token']},
                          method='POST')
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def local(self, url):
        parsed = urlparse(url)
        path = parsed.path
        if path.startswith('/task/'):
            path = path[len('/task'):]
        return self.base + path + (('?' + parsed.query) if parsed.query else '')

    def raw_get(self, url):
        try:
            with urlopen(url, timeout=30) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def mcp(self, payload, sid=None):
        headers = {'Content-Type': 'application/json'}
        if sid:
            headers['Mcp-Session-Id'] = sid
        with urlopen(Request(self.base + '/v1/mcp', data=json.dumps(payload).encode(),
                             headers=headers, method='POST'), timeout=60) as response:
            return json.load(response), response.headers.get('Mcp-Session-Id')

    def mcp_call(self, name, args, sid):
        response, _ = self.mcp({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                'params': {'name': 'diaodu_' + name, 'arguments': args}}, sid)
        result = response['result']
        return result, json.loads(result['content'][0]['text'])

    def test_visibility_between_workers(self):
        broadcast = self.send('mentor', 'instruction', '全体指令', 'inst-all')[1]['event']
        targeted = self.send('mentor', 'instruction', '只给二号', 'inst-2',
                             to=f"worker:{self.workers['w2']}")[1]['event']
        w1_seqs = [event['seq'] for event in self.events('w1')]
        w2_seqs = [event['seq'] for event in self.events('w2')]
        self.assertIn(broadcast['seq'], w1_seqs)
        self.assertNotIn(targeted['seq'], w1_seqs)
        self.assertIn(targeted['seq'], w2_seqs)
        result = self.send('w2', 'result', '二号结果', 'r2', reply_to=targeted['seq'])[1]['event']
        self.assertNotIn(result['seq'], [event['seq'] for event in self.events('w1')])
        self.assertIn(result['seq'], [event['seq'] for event in self.events('mentor')])
        note = self.send('w1', 'note', '私有备注', 'n1')[1]['event']
        self.assertNotIn(note['seq'], [event['seq'] for event in self.events('w3')])
        public = self.send('w1', 'note', '公开备注', 'n1-all', to='all')[1]['event']
        self.assertIn(public['seq'], [event['seq'] for event in self.events('w3')])
        status, body = self.send('w1', 'result', '跨席', 'r-cross',
                                 to=f"worker:{self.workers['w2']}")
        self.assertEqual(status, 403)

    def test_invites_single_use_expired_revoked_and_ledger(self):
        first = self.invite('worker', note='临时帮手')
        status, _ = self.req('POST', '/v1/relay/teams/join',
                             {'code': first['code'], 'environment': dict(ENVIRONMENT)}, 'outsider')
        self.assertEqual(status, 200)
        status, body = self.req('POST', '/v1/relay/teams/join',
                                {'code': first['code'], 'environment': dict(ENVIRONMENT)}, 'w3')
        self.assertEqual(status, 409)
        ledger = self.req('GET', f'/v1/relay/teams/{self.sid}/invites', role='owner')[1]['invites']
        record = next(item for item in ledger if item['id'] == first['id'])
        self.assertEqual(record['status'], 'used')
        self.assertEqual(record['used_by']['name'], 'outsider')
        self.assertEqual(record['note'], '临时帮手')
        expired = self.invite('worker')
        with sqlite3.connect(self.library.teams.path) as db:
            db.execute('UPDATE team_invites SET expires=0 WHERE id=?', (expired['id'],))
        self.assertEqual(self.req('POST', '/v1/relay/teams/join',
                                  {'code': expired['code'],
                                   'environment': dict(ENVIRONMENT)}, 'w3')[0], 403)
        revoked = self.invite('worker')
        self.req('POST', f"/v1/relay/teams/{self.sid}/invites/{revoked['id']}/revoke", {}, 'owner')
        self.assertEqual(self.req('POST', '/v1/relay/teams/join',
                                  {'code': revoked['code'],
                                   'environment': dict(ENVIRONMENT)}, 'w3')[0], 403)
        self.assertEqual(self.req('POST', '/v1/relay/teams/join',
                                  {'code': 'nope', 'environment': dict(ENVIRONMENT)}, 'w3')[0], 403)

    def test_revoke_seat_invalidates_invite_and_blocks_rejoin(self):
        pending = self.invite('worker', seat=4)
        self.assertEqual(self.req('POST', '/v1/relay/teams/join',
                                  {'code': pending['code'],
                                   'environment': dict(ENVIRONMENT)}, 'outsider')[0], 200)
        status, _ = self.req('POST', f'/v1/relay/teams/{self.sid}/revoke',
                             {'role': 'worker', 'seat': 4}, 'owner')
        self.assertEqual(status, 200)
        self.assertEqual(self.req('GET', f'/v1/relay/teams/{self.sid}', role='outsider')[0], 404)
        self.assertEqual(self.send('mentor', 'instruction', '已撤销', 'inst-rev',
                                   to='worker:4')[0], 409)
        fresh = self.invite('worker')
        self.assertEqual(self.req('POST', '/v1/relay/teams/join',
                                  {'code': fresh['code'],
                                   'environment': dict(ENVIRONMENT)}, 'outsider')[0], 403)

    def test_team_attachments_signed_and_private(self):
        content = b'\x89PNG\r\n\x1a\n' + b'team-bytes' * 100
        status, meta = self.upload('w1', '团队图.png', content)
        self.assertEqual(status, 201)
        self.assertTrue(meta['url'].startswith(
            f"https://www.chuhaijian.com/task/v1/relay/teams/{self.sid}/files/"))
        self.assertIn('?exp=', meta['url'])
        request = Request(self.base + f"/v1/relay/teams/{self.sid}/files/{meta['id']}",
                          headers={'Authorization': 'Bearer ' + self.actors['mentor']['token']})
        with urlopen(request, timeout=30) as response:
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(), meta['sha256'])
        status, data = self.raw_get(self.local(meta['url']))
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(data).hexdigest(), meta['sha256'])
        status, _ = self.raw_get(self.base + f"/v1/relay/teams/{self.sid}/files/{meta['id']}")
        self.assertEqual(status, 401)
        other = self.req('POST', '/v1/relay/teams', {'title': '别人的团队', 'goal': 'x'}, 'outsider')[1]
        request = Request(self.base + f"/v1/relay/teams/{other['session']['id']}/files/{meta['id']}",
                          headers={'Authorization': 'Bearer ' + self.actors['outsider']['token']})
        with self.assertRaises(HTTPError) as denied:
            urlopen(request, timeout=30)
        self.assertEqual(denied.exception.code, 404)

    def test_invite_public_info_has_no_credentials(self):
        invite = self.invite('worker', note='给 Bob')
        status, info = self.req('GET', '/v1/relay/teams/invites/' + invite['code'])
        self.assertEqual(status, 200)
        self.assertEqual(info['title'], '演示团队')
        self.assertEqual(info['invite']['status'], 'unused')
        self.assertEqual(info['invite']['note'], '给 Bob')
        self.assertTrue(info['mcp_url'].endswith('/task/v1/mcp'))
        serialized = json.dumps(info)
        for leak in ('token', 'actor_key', 'ag_', 'us_', 'tm_'):
            self.assertNotIn(leak, serialized)
        self.assertEqual(self.req('GET', '/v1/relay/teams/invites/nope')[0], 404)

    def test_mcp_team_flow_invite_join_send_ack_and_download(self):
        init, sid = self.mcp({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {}})
        self.mcp_call('connect', {'alias': 'mcp-owner', 'agent_type': 'Dev'}, sid)
        created_already = self.req('GET', f'/v1/relay/teams/{self.sid}', role='owner')[1]
        result, payload = self.mcp_call('relay_team_get', {'session': self.sid}, sid)
        self.assertTrue(result.get('isError'))
        # 用 owner Bearer 不适合 MCP 会话身份，换一个团队让 MCP 自己建。
        result, created = self.mcp_call('relay_team_create', {'title': 'MCP 团队', 'goal': '工具链路'}, sid)
        self.assertFalse(result.get('isError'))
        team_sid = created['session']['id']
        result, invite = self.mcp_call('relay_team_invite', {'session': team_sid, 'role': 'worker'}, sid)
        self.assertFalse(result.get('isError'))
        code = invite['invite']['code']
        init2, sid2 = self.mcp({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {}})
        self.mcp_call('connect', {'alias': 'mcp-worker', 'agent_type': 'Dev'}, sid2)
        result, joined = self.mcp_call('relay_team_join',
                                       {'code': code, 'environment': dict(ENVIRONMENT)}, sid2)
        self.assertFalse(result.get('isError'))
        self.assertEqual(joined['session']['seat'], 1)
        result, _ = self.mcp_call('relay_team_send', {'session': team_sid, 'kind': 'progress',
                                                      'text': '开工', 'request_id': 'm1'}, sid2)
        self.assertFalse(result.get('isError'))
        result, payload = self.mcp_call('relay_team_read', {'session': team_sid, 'wait': 0}, sid2)
        self.assertFalse(result.get('isError'))
        self.assertTrue(any(event['kind'] == 'message' for event in payload['events']))
        result, ack = self.mcp_call('relay_team_ack', {'session': team_sid, 'seq': payload['next_cursor']}, sid2)
        self.assertFalse(result.get('isError'))
        self.assertEqual(ack['acknowledged'], payload['next_cursor'])
        big = b'x' * (5 * 1024 * 1024 + 1)
        result, uploaded = self.mcp_call('relay_team_upload', {
            'session': team_sid, 'name': 'big.bin', 'content_base64': base64.b64encode(big).decode()}, sid2)
        self.assertFalse(result.get('isError'))
        result, downloaded = self.mcp_call('relay_team_download', {
            'session': team_sid, 'file_id': uploaded['id']}, sid2)
        self.assertTrue(downloaded['too_large'])
        self.assertNotIn('content_base64', downloaded)
        status, data = self.raw_get(self.local(downloaded['download_url']))
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(data).hexdigest(), uploaded['sha256'])


if __name__ == '__main__':
    unittest.main()
