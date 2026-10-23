import base64
import hashlib
import io
import json
import socket
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import test_relay_http as fixtures


class RelayFiles(unittest.TestCase):
    setUp = fixtures.RelayHTTP.setUp
    tearDown = fixtures.RelayHTTP.tearDown
    req = fixtures.RelayHTTP.req
    join = fixtures.RelayHTTP.join
    mcp_request = fixtures.RelayHTTP.mcp_request
    mcp_initialize = fixtures.RelayHTTP.mcp_initialize
    mcp_call = fixtures.RelayHTTP.mcp_call
    mcp_payload = staticmethod(fixtures.RelayHTTP.mcp_payload)

    def upload(self, sid, name, content, role=None):
        headers = {'Content-Type': 'application/octet-stream'}
        if role:
            headers['Authorization'] = 'Bearer ' + self.identities[role]['token']
        request = Request(self.base + f'/v1/relay/sessions/{sid}/files?name=' + quote(name),
                          data=content, headers=headers, method='POST')
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def download(self, sid, file_id, role=None):
        headers = {}
        if role:
            headers['Authorization'] = 'Bearer ' + self.identities[role]['token']
        request = Request(self.base + f'/v1/relay/sessions/{sid}/files/{file_id}', headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.read(), response.headers
        except HTTPError as error:
            with error:
                return error.code, error.read(), error.headers

    def test_upload_download_and_sha256(self):
        self.join()
        content = b'\x89PNG\r\n\x1a\n' + b'report-bytes' * 10
        status, meta = self.upload(self.sid, '报表.png', content, 'worker')
        self.assertEqual(status, 201)
        self.assertEqual(meta['sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(meta['size'], len(content))
        self.assertEqual(meta['name'], '报表.png')
        self.assertEqual(meta['type'], 'image/png')
        self.assertNotIn('actor_key', meta)
        self.assertTrue(meta['url'].endswith(f'/v1/relay/sessions/{self.sid}/files/{meta["id"]}'))
        status, data, headers = self.download(self.sid, meta['id'], 'mentor')
        self.assertEqual(status, 200)
        self.assertEqual(data, content)
        self.assertEqual(headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(headers.get('Content-Type'), 'image/png')
        self.assertIn('inline', headers.get('Content-Disposition'))

    def test_content_type_requires_extension_and_magic_agreement(self):
        self.join()
        status, meta = self.upload(self.sid, '假装图片.png', b'<svg onload=alert(1)>', 'worker')
        self.assertEqual(status, 201)
        self.assertEqual(meta['type'], 'application/octet-stream')
        status, data, headers = self.download(self.sid, meta['id'], 'mentor')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Type'), 'application/octet-stream')
        self.assertIn('attachment', headers.get('Content-Disposition'))
        status, meta = self.upload(self.sid, '日志.txt', b'line1\nline2\n', 'worker')
        self.assertEqual(meta['type'], 'text/plain')
        status, _, headers = self.download(self.sid, meta['id'], 'mentor')
        self.assertEqual(headers.get('Content-Type'), 'text/plain')

    def test_filename_rejects_paths_and_long_names(self):
        self.join()
        for name in ('../escape.txt', 'a/b.txt', 'a\\b.txt', 'x' * 241, '   '):
            with self.subTest(name=name):
                status, _ = self.upload(self.sid, name, b'x', 'worker')
                self.assertEqual(status, 400)

    def test_private_attachments_and_cross_session_ids(self):
        self.join()
        status, _ = self.upload(self.sid, '私有.txt', b'secret', 'outsider')
        self.assertEqual(status, 403)
        status, meta = self.upload(self.sid, '私有.txt', b'secret', 'worker')
        self.assertEqual(status, 201)
        status, _, _ = self.download(self.sid, meta['id'], 'outsider')
        self.assertEqual(status, 403)
        other = self.clients['worker'].create('另一个会话', '跨会话不可读')
        other_sid = other['session']['id']
        status, _, _ = self.download(other_sid, meta['id'], 'worker')
        self.assertEqual(status, 404)

    def test_oversize_is_rejected_without_leaving_files(self):
        self.join()
        relay = self.library.relay
        actor = self.identity.agent(self.identities['worker']['token'])
        with self.assertRaises(ValueError):
            relay.receive(actor, self.sid, 'big.bin', io.BytesIO(b'x'), relay.MAX_FILE_BYTES + 1)
        self.assertEqual(list(relay.files_dir.iterdir()), [])
        token = self.identities['worker']['token'].encode()
        request = (f'POST /v1/relay/sessions/{self.sid}/files?name=x.bin HTTP/1.1\r\n'
                   f'Host: test\r\nAuthorization: Bearer {token.decode()}\r\n'
                   f'Content-Length: {relay.MAX_FILE_BYTES + 1}\r\nConnection: close\r\n\r\n').encode()
        with socket.create_connection(('127.0.0.1', self.server.server_address[1]), timeout=5) as sock:
            sock.sendall(request)
            response = sock.recv(200)
        self.assertIn(b' 400 ', response.split(b'\r\n')[0])
        self.assertEqual(list(relay.files_dir.iterdir()), [])

    def test_send_rejects_unknown_and_too_many_attachments(self):
        self.join()
        path = f'/v1/relay/sessions/{self.sid}/messages'
        self.assertEqual(self.req('POST', path, {'kind': 'note', 'text': 'x', 'request_id': 'u1',
                                                 'attachments': ['fl_missing']}, 'mentor')[0], 400)
        self.assertEqual(self.req('POST', path, {'kind': 'note', 'text': 'x', 'request_id': 'u2',
                                                 'attachments': ['fl_x'] * 21}, 'mentor')[0], 400)

    def test_old_messages_read_with_empty_attachments(self):
        self.join()
        self.clients['mentor'].send(self.sid, 'note', '无附件消息', 'no-attachment')
        events = self.clients['mentor'].events(self.sid, wait=0)['events']
        message = next(event['message'] for event in events
                       if event.get('message', {}).get('text') == '无附件消息')
        self.assertEqual(message['attachments'], [])

    def test_mcp_upload_and_send_attachment(self):
        sid = self.mcp_initialize()
        self.mcp_call('connect', {'alias': 'MCP 文件端', 'agent_type': 'Dev'}, sid)
        status, body = self.mcp_call('relay_create', {'title': 'MCP 文件', 'goal': '附件链路'}, sid)
        created = self.mcp_payload(body)
        sid2 = self.mcp_initialize()
        self.mcp_call('connect', {'alias': 'MCP 执行端', 'agent_type': 'Dev'}, sid2)
        self.mcp_call('relay_join', {'code': created['topic_code'], 'role': 'worker',
                                     'environment': {'os_name': 'macOS', 'os_version': 'test',
                                                     'architecture': 'arm64'}}, sid2)
        content = b'MCP file body'
        status, body = self.mcp_call('relay_upload', {
            'session': created['session']['id'], 'name': '结果.txt',
            'content_base64': base64.b64encode(content).decode()}, sid2)
        self.assertFalse(body['result']['isError'])
        meta = self.mcp_payload(body)
        self.assertEqual(meta['sha256'], hashlib.sha256(content).hexdigest())
        status, body = self.mcp_call('relay_send', {
            'session': created['session']['id'], 'kind': 'result', 'text': '完成',
            'request_id': 'mcp-file-1', 'attachments': [meta['id']]}, sid2)
        event = self.mcp_payload(body)['event']
        self.assertEqual(event['message']['attachments'][0]['sha256'], meta['sha256'])
        self.assertEqual(event['message']['attachments'][0]['name'], '结果.txt')


if __name__ == '__main__':
    unittest.main()
