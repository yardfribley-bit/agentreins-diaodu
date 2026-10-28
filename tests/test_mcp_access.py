"""Durable identity, email bind, and MCP discovery."""
import json
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import test_identity as fixtures
from diaodu.discovery import DESIGN_MCP, GUIDE, HUB_MCP
from diaodu.identity import Identity


class McpAccess(unittest.TestCase):
    setUp = fixtures.IdentityAPI.setUp
    tearDown = fixtures.IdentityAPI.tearDown
    req = fixtures.IdentityAPI.req

    def raw(self, method, path, body=None, headers=None):
        h = {} if headers is None else dict(headers)
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urlopen(request, timeout=8) as response:
                raw = response.read()
                return response.status, self._parse(raw, response.headers.get('Content-Type', '')), response.headers
        except HTTPError as exc:
            with exc:
                raw = exc.read()
                return exc.code, self._parse(raw, exc.headers.get('Content-Type', '')), exc.headers

    @staticmethod
    def _parse(raw, content_type):
        text = raw.decode()
        if 'json' in content_type or (text[:1] in '{['):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text

    def rpc(self, method, params=None, sid=None, token=None, call=None):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}
        headers = {'Content-Type': 'application/json'}
        if sid:
            headers['Mcp-Session-Id'] = sid
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if call:
            body = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'diaodu_' + method, 'arguments': params or {}}}
        status, payload, headers_out = self.raw('POST', '/v1/mcp', body, headers)
        self.assertEqual(status, 200, payload)
        return payload, headers_out.get('Mcp-Session-Id')

    def tool(self, name, args, sid=None, token=None):
        payload, _sid = self.rpc(name, args, sid=sid, token=token, call=True)
        self.assertFalse(payload['result']['isError'], payload)
        return json.loads(payload['result']['content'][0]['text'])

    def test_anonymous_id_is_stable_across_reconnect(self):
        status, first = self.req('POST', '/v1/connect', {'alias': '持久', 'agent_type': 'Codex'})
        self.assertEqual(status, 201)
        self.assertTrue(first['token'].startswith('da_'))
        self.assertEqual(first['account_key'], first['user_id'])
        self.assertFalse(first['email_bound'])
        reopened = Identity(self.identity.path, 'test-invite')
        again = reopened.agent(first['token'])
        self.assertEqual(again['agent_id'], first['agent_id'])
        self.assertEqual(again['user_id'], first['user_id'])
        status, me = self.req('GET', '/v1/me', headers={'Authorization': 'Bearer ' + first['token']})
        self.assertEqual(status, 200)
        self.assertEqual(me['agent_id'], first['agent_id'])

        init, sid = self.rpc('initialize', {'protocolVersion': '2025-11-25'})
        self.assertIn('DIAODU_AGENT_TOKEN', init['result']['instructions'])
        self.assertIn('email', init['result']['instructions'].lower())
        self.assertIn(GUIDE, init['result']['instructions'])
        self.assertIn(DESIGN_MCP, init['result']['instructions'])
        connected = self.tool('connect', {'alias': 'MCP持久', 'agent_type': 'Claude'}, sid)
        self.assertTrue(connected['token'].startswith('da_'))
        self.assertIn('DIAODU_AGENT_TOKEN', connected['credential_storage'])
        self.assertEqual(connected['account_key'], connected['user_id'])
        _init2, sid2 = self.rpc('initialize', {'protocolVersion': '2025-11-25'})
        reused = self.tool('connect', {'alias': 'MCP持久', 'agent_type': 'Claude'}, sid2, token=connected['token'])
        self.assertTrue(reused['reused'])
        self.assertNotIn('token', reused)
        self.assertEqual(reused['agent_id'], connected['agent_id'])
        self.assertEqual(reused['user_id'], connected['user_id'])

    def test_email_bind_keeps_history_and_conflict_does_not_merge(self):
        status, first = self.req('POST', '/v1/connect', {'alias': '先匿名', 'agent_type': 'Cursor'})
        h = {'Authorization': 'Bearer ' + first['token']}
        status, task = self.req('POST', '/v1/tasks', {'requirement': '绑定前的任务'}, headers=h)
        self.assertEqual(status, 201)
        status, bad = self.req('POST', '/v1/me/email', {'email': 'not-an-email', 'contact_consent': True}, headers=h)
        self.assertEqual(status, 400)
        status, bound = self.req('POST', '/v1/me/email', {'email': 'Owner@Example.com', 'contact_consent': True}, headers=h)
        self.assertEqual(status, 200)
        self.assertEqual(bound['account_key'], 'owner@example.com')
        self.assertEqual(bound['prior_account_key'], first['user_id'])
        self.assertTrue(bound['history_kept'])
        self.assertFalse(bound['reused'])
        status, task_after = self.req('GET', '/v1/tasks/' + task['number'])
        self.assertEqual(task_after['publisher']['user_id'], first['user_id'])
        self.assertEqual(task_after['publisher']['agent_id'], first['agent_id'])
        self.assertNotIn('owner@example.com', json.dumps(task_after))
        status, me = self.req('GET', '/v1/me', headers=h)
        self.assertNotIn('owner@example.com', json.dumps(me))
        status, activity = self.req('GET', '/v1/me/activity', headers=h)
        actions = [op['action'] for op in activity['ops']]
        self.assertIn('connect', actions)
        self.assertIn('bind_email', actions)
        self.assertEqual(activity['account_key'], 'owner@example.com')
        with self.identity.db() as db:
            alias = db.execute('SELECT user_id, kind FROM identity_aliases WHERE alias=?', (first['user_id'],)).fetchone()
        self.assertEqual(alias['user_id'], first['user_id'])
        self.assertEqual(alias['kind'], 'prior_id')
        status, again = self.req('POST', '/v1/me/email', {'email': 'OWNER@example.com', 'contact_consent': True}, headers=h)
        self.assertEqual(status, 200)
        self.assertTrue(again['reused'])
        self.assertEqual(again['account_key'], 'owner@example.com')

        status, other = self.req('POST', '/v1/connect', {'alias': '另一个', 'agent_type': 'Codex'})
        h2 = {'Authorization': 'Bearer ' + other['token']}
        status, conflict = self.req('POST', '/v1/me/email', {'email': 'owner@example.com', 'contact_consent': True}, headers=h2)
        self.assertEqual(status, 409)
        self.assertEqual(conflict['code'], 'email_in_use')
        self.assertIn('不会合并', conflict['error'])
        status, me2 = self.req('GET', '/v1/me', headers=h2)
        self.assertEqual(me2['user_id'], other['user_id'])
        self.assertNotEqual(me2['user_id'], first['user_id'])
        status, act2 = self.req('GET', '/v1/me/activity', headers=h2)
        self.assertNotIn('bind_email', [op['action'] for op in act2['ops']])
        status, still = self.req('GET', '/v1/tasks/' + task['number'])
        self.assertEqual(still['publisher']['user_id'], first['user_id'])
        status, mine = self.req('GET', '/v1/me/activity', headers=h)
        self.assertEqual([op['action'] for op in mine['ops']].count('bind_email'), 1)

    def test_discovery_files_guide_tools_and_relay_docs(self):
        for path in ('/llms.txt', '/.well-known/mcp.json', '/v1/llms.txt', '/v1/.well-known/mcp.json'):
            status, body, _headers = self.raw('GET', path)
            self.assertEqual(status, 200, path)
            blob = body if isinstance(body, str) else json.dumps(body)
            self.assertIn(HUB_MCP, blob, path)
            self.assertIn(DESIGN_MCP, blob, path)
        status, card, _headers = self.raw('GET', '/.well-known/mcp.json')
        self.assertEqual(card['mcpServers']['agentreins-hub']['url'], HUB_MCP)
        self.assertEqual(card['mcpServers']['agentreins-design']['url'], DESIGN_MCP)
        self.assertEqual(card['mcpServers']['agentreins-hub']['authentication']['env'], 'DIAODU_AGENT_TOKEN')

        status, guide, _headers = self.raw('GET', '/v1/agent-guide.md')
        self.assertEqual(status, 200)
        self.assertIsInstance(guide, str)
        self.assertLess(guide.find('DIAODU_AGENT_TOKEN'), guide.find('实时任务大厅'))
        self.assertLess(guide.find('contact_consent'), guide.find('实时任务大厅'))

        status, page, _headers = self.raw('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('id="mcp-discovery"', page)
        self.assertIn(HUB_MCP, page)
        self.assertIn('agent-guide.md', page)
        script = (Path(__file__).resolve().parents[1] / 'static' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('DIAODU_AGENT_TOKEN', script)
        self.assertIn('bearer_token_env_var', script)
        self.assertIn('agentreins-design', script)
        self.assertIn(DESIGN_MCP, script)

        listed, _sid = self.rpc('tools/list')
        tools = {tool['name']: tool for tool in listed['result']['tools']}
        self.assertIn('DIAODU_AGENT_TOKEN', tools['diaodu_connect']['description'])
        self.assertIn('email', tools['diaodu_bind_email']['description'].lower())
        self.assertIn('409', tools['diaodu_bind_email']['description'])
        self.assertIn('offset', tools['diaodu_list_tasks']['inputSchema']['properties'])
        self.assertIn('description', tools['diaodu_list_tasks']['inputSchema']['properties']['offset'])
        self.assertIn('description', tools['diaodu_connect']['inputSchema']['properties']['agent_type'])
        self.assertIn('description', tools['diaodu_read_task']['inputSchema']['properties']['task'])
        self.assertIn('diaodu_join', tools['diaodu_read_chat']['description'])
        self.assertTrue(tools['diaodu_read_chat']['annotations']['readOnlyHint'])
        self.assertTrue(tools['diaodu_relay_team_revoke']['annotations']['destructiveHint'])
        self.assertIn('description', tools['diaodu_relay_team_gate_join']['inputSchema']['properties']['gate_code'])
        self.assertIn('diaodu_create_task', tools)

        init, sid = self.rpc('initialize', {})
        page = self.tool('list_tasks', {'limit': 1, 'offset': 0}, sid)
        self.assertEqual(page['returned_tasks'], 1)
        self.assertIn('next_offset', page)
        empty = self.tool('list_tasks', {'limit': 1, 'offset': 50}, sid)
        self.assertEqual(empty['returned_tasks'], 0)
        self.assertIsNone(empty['next_offset'])
        bad, _sid = self.rpc('list_tasks', {'offset': -1}, sid, call=True)
        self.assertTrue(bad['result']['isError'])
        message = bad['result']['content'][0]['text']
        self.assertNotIn('不允许 offset', message)
        self.assertIn('offset', message)

        for path in ('/v1/relay/relay-teams.md', '/v1/relay/relay-work.md'):
            status, text, _headers = self.raw('GET', path)
            self.assertEqual(status, 200, path)
            self.assertIsInstance(text, str)
            self.assertGreater(len(text), 80)
        status, authed, _headers = self.raw('GET', '/v1/relay/relay-teams.md', headers={'Authorization': 'Bearer ' + self.agents[0]['token']})
        self.assertEqual(status, 200)
        self.assertIn('门禁', authed)
        status, work, _headers = self.raw('GET', '/v1/relay/relay-work.md')
        self.assertIn('领导者', work)

    def test_read_chat_does_not_join_until_join_tool(self):
        _init, sid = self.rpc('initialize', {})
        person = self.tool('connect', {'alias': '只读', 'agent_type': 'Codex'}, sid)
        refused, _sid = self.rpc('read_chat', {'task': self.task['number']}, sid, call=True)
        self.assertTrue(refused['result']['isError'])
        self.assertIn('diaodu_join', refused['result']['content'][0]['text'])
        status, chat = self.req('GET', '/v1/tasks/' + self.task['number'] + '/chat', user=0)
        self.assertEqual(status, 200)
        self.assertFalse(any('加入了任务' in message.get('text', '') for message in chat['messages']))
        joined = self.tool('join', {'task': self.task['number']}, sid)
        self.assertTrue(joined['joined'])
        status, chat = self.req('GET', '/v1/tasks/' + self.task['number'] + '/chat', user=0)
        joins = [message for message in chat['messages'] if message.get('text') == '只读 加入了任务']
        self.assertEqual(len(joins), 1)
        read = self.tool('read_chat', {'task': self.task['number']}, sid)
        self.assertEqual(sum(message.get('text') == '只读 加入了任务' for message in read['messages']), 1)
        self.tool('read_chat', {'task': self.task['number'], 'after': read['next_cursor']}, sid)
        status, chat = self.req('GET', '/v1/tasks/' + self.task['number'] + '/chat', user=0)
        self.assertEqual(sum(message.get('text') == '只读 加入了任务' for message in chat['messages']), 1)
        created = self.tool('create_task', {'requirement': 'MCP 发布的任务'}, sid)
        self.assertTrue(created['number'].startswith('T'))
        self.assertEqual(created['publisher']['agent_id'], person['agent_id'])
        self.assertNotIn('token', json.dumps(created))

    def test_mcp_auth_errors_are_jsonrpc_and_actionable(self):
        status, body, headers = self.raw('POST', '/v1/mcp', {
            'jsonrpc': '2.0', 'id': 7, 'method': 'initialize', 'params': {},
        }, {'Authorization': 'Bearer da_not-a-token', 'Content-Type': 'application/json'})
        self.assertEqual(status, 401)
        self.assertEqual(body['jsonrpc'], '2.0')
        self.assertEqual(body['id'], 7)
        self.assertEqual(body['code'], 'unauthenticated')
        self.assertIn('DIAODU_AGENT_TOKEN', body['error']['message'])
        self.assertIn('Bearer', headers['WWW-Authenticate'])
        self.assertIn('mcp.json', headers['WWW-Authenticate'])
        status, body, headers = self.raw('POST', '/v1/mcp/design/', {
            'jsonrpc': '2.0', 'id': 3, 'method': 'initialize', 'params': {},
        }, {'Content-Type': 'application/json'})
        self.assertEqual(status, 401)
        self.assertEqual(body['jsonrpc'], '2.0')
        self.assertEqual(body['code'], 'unauthenticated')
        self.assertIn('connect', body['error']['message'])
        self.assertIn('Bearer', headers['WWW-Authenticate'])
        status, pointer = self.req('GET', '/v1/mcp')
        self.assertEqual(status, 405)
        self.assertEqual(pointer['code'], 'method_not_allowed')
        self.assertIn(HUB_MCP, json.dumps(pointer))
        status, deleted, _headers = self.raw('DELETE', '/v1/mcp')
        self.assertEqual(status, 200)
        self.assertEqual(deleted['ok'], True)


if __name__ == '__main__':
    unittest.main()
