import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import test_relay_http as fixtures
from diaodu import relay_http

FORBIDDEN_KEYS = ("environment", "os_name", "os_version", "architecture", "shells",
                  "python_version", "actor_key", "token", "sig", "download_url")


class RelayPublic(unittest.TestCase):
    setUp = fixtures.RelayHTTP.setUp
    tearDown = fixtures.RelayHTTP.tearDown
    req = fixtures.RelayHTTP.req
    join = fixtures.RelayHTTP.join

    def set_public(self, enabled, role='owner'):
        status, body = self.req('POST', f'/v1/relay/sessions/{self.sid}/public',
                                {'enabled': enabled}, role)
        return status, body

    def token_from(self, url):
        return urlparse(url).path.rstrip('/').split('/')[-1]

    def public_get(self, token, after=0, wait=0):
        try:
            with urlopen(self.base + f'/v1/relay/public/{quote(token, safe="")}/events'
                                     f'?after={after}&wait={wait}', timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def upload(self, name, content, role='worker'):
        request = Request(self.base + f'/v1/relay/sessions/{self.sid}/files?name=' + quote(name),
                          data=content, headers={'Content-Type': 'application/octet-stream',
                                                 'Authorization': 'Bearer ' + self.identities[role]['token']},
                          method='POST')
        with urlopen(request, timeout=10) as response:
            return response.status, json.load(response)

    def test_public_view_filters_environment_and_attachment_links(self):
        self.join()
        changed = {"os_name": "Windows", "os_version": "11 / 10.0.26100", "architecture": "AMD64",
                   "shells": ["powershell"], "python_version": "3.12.3"}
        self.req('POST', f'/v1/relay/sessions/{self.sid}/heartbeat', {'environment': changed}, 'worker')
        content = b'\x89PNG\r\n\x1a\n' + b'public-bytes' * 20
        status, meta = self.upload('公开图.png', content)
        self.assertEqual(status, 201)
        self.req('POST', f'/v1/relay/sessions/{self.sid}/messages',
                 {'kind': 'note', 'text': '带附件', 'request_id': 'pub-1',
                  'attachments': [meta['id']]}, 'mentor')
        member_kinds = [event['kind'] for event in self.clients['mentor'].events(self.sid, wait=0)['events']]
        self.assertIn('environment', member_kinds)
        status, body = self.set_public(True)
        self.assertEqual(status, 200)
        token = self.token_from(body['public_url'])
        status, public = self.public_get(token)
        self.assertEqual(status, 200)
        kinds = [event['kind'] for event in public['events']]
        self.assertNotIn('environment', kinds)
        self.assertIn('message', kinds)
        serialized = json.dumps(public, ensure_ascii=False)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn('"' + key + '"', serialized)
        self.assertNotIn(self.sid, serialized)
        attachments = [attachment for event in public['events'] for attachment in
                       (event.get('message', {}).get('attachments') or [])]
        self.assertTrue(attachments)
        self.assertEqual(set(attachments[0]), {'name', 'size', 'type'})
        joined = [event for event in public['events'] if event['kind'] == 'joined']
        self.assertTrue(joined)
        for event in joined:
            self.assertEqual(set(event) - {'seq', 'kind', 'created_at', 'sender', 'role'}, set())

    def test_enable_disable_and_rotation_invalidate_old_links(self):
        self.join()
        status, missing = self.public_get('not-a-token')
        self.assertEqual(status, 404)
        status, first = self.set_public(True)
        token1 = self.token_from(first['public_url'])
        self.assertTrue(self.clients['owner'].get(self.sid)['session']['public_read'])
        self.assertEqual(self.public_get(token1)[0], 200)
        self.assertEqual(self.set_public(False)[0], 200)
        self.assertFalse(self.clients['owner'].get(self.sid)['session']['public_read'])
        self.assertEqual(self.public_get(token1)[0], 404)
        status, second = self.set_public(True)
        token2 = self.token_from(second['public_url'])
        self.assertNotEqual(token1, token2)
        self.assertEqual(self.public_get(token1)[0], 404)
        self.assertEqual(self.public_get(token2)[0], 200)
        self.assertEqual(self.set_public(True, role='mentor')[0], 403)

    def test_public_endpoint_is_read_only_and_closed_session_visible(self):
        self.join()
        _, body = self.set_public(True)
        token = self.token_from(body['public_url'])
        request = Request(self.base + f'/v1/relay/public/{quote(token, safe="")}/events',
                          data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urlopen(request, timeout=10) as response:
                self.fail('公开接口不应接受写请求')
        except HTTPError as error:
            self.assertIn(error.code, (401, 403, 404, 405))
        self.clients['owner'].state(self.sid, 'closed')
        status, public = self.public_get(token)
        self.assertEqual(status, 200)
        self.assertEqual(public['session']['status'], 'closed')
        self.assertNotIn(self.sid, json.dumps(public))

    def test_disabling_wakes_public_long_poll(self):
        self.join()
        _, body = self.set_public(True)
        token = self.token_from(body['public_url'])
        last_seq = self.clients['owner'].get(self.sid)['session']['last_seq']
        result = {}
        def wait_public():
            result['value'] = self.public_get(token, after=last_seq, wait=5)
        thread = threading.Thread(target=wait_public, daemon=True)
        thread.start()
        time.sleep(0.3)
        self.set_public(False)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result['value'][0], 404)

    def test_public_rate_limit_is_stricter(self):
        self.join()
        _, body = self.set_public(True)
        token = self.token_from(body['public_url'])
        with relay_http._HITS_LOCK:
            relay_http._PUBLIC_HITS.clear()
        with patch.object(relay_http, 'PUBLIC_RATE_LIMIT', 1):
            self.assertEqual(self.public_get(token)[0], 200)
            self.assertEqual(self.public_get(token)[0], 429)
        with relay_http._HITS_LOCK:
            relay_http._PUBLIC_HITS.clear()

    def test_watch_page_and_script_are_public(self):
        _, body = self.set_public(True)
        token = self.token_from(body['public_url'])
        request = Request(self.base + f'/relay/watch/{token}')
        with urlopen(request, timeout=10) as response:
            page = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn('text/html', response.headers.get('Content-Type'))
        self.assertIn('环境信息不对外展示', page)
        with urlopen(self.base + '/relay-watch.js', timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('javascript', response.headers.get('Content-Type'))


if __name__ == '__main__':
    unittest.main()
