#!/usr/bin/env python3
"""One short gate code seats three workers. The fourth join is rejected.

The owner opens a single worker gate (no per-seat invite paste). Each worker
posts the same short code plus its own environment. Closing the gate stops
further joins. A one-time mentor invite still works beside the gate.

    python3 scripts/team_gate_demo.py
"""
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from diaodu.identity import Identity  # noqa: E402
from diaodu.library import Library  # noqa: E402
from diaodu.server import make_handler  # noqa: E402

ENVIRONMENTS = {
    'w1': {'os_name': 'DemoOS-1', 'os_version': '1.0', 'architecture': 'demo-arch-1',
           'shells': ['demo-shell-1'], 'python_version': '3.11.1'},
    'w2': {'os_name': 'DemoOS-2', 'os_version': '2.0', 'architecture': 'demo-arch-2',
           'shells': ['demo-shell-2'], 'python_version': '3.11.2'},
    'w3': {'os_name': 'DemoOS-3', 'os_version': '3.0', 'architecture': 'demo-arch-3',
           'shells': ['demo-shell-3'], 'python_version': '3.11.3'},
    'w4': {'os_name': 'DemoOS-4', 'os_version': '4.0', 'architecture': 'demo-arch-4',
           'shells': ['demo-shell-4'], 'python_version': '3.11.4'},
}


class Client:
    def __init__(self, base, token=None):
        self.base = base.rstrip('/')
        self.token = token

    def request(self, method, path, body=None):
        headers = {'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def get(self, path):
        return self.request('GET', path)

    def post(self, path, body):
        return self.request('POST', path, body)


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print('  ✓ ' + message)


def main():
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    library = Library(root, '/task')
    identity = Identity(root / 'identity.sqlite3', '')
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(None, library, identity))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_address[1])
    started = time.monotonic()
    try:
        print('AgentReins Hub 团队门禁演示 · ' + base + '/relay/')
        actors = {name: identity.connect(name, '演示Agent')
                  for name in ('owner', 'mentor', 'w1', 'w2', 'w3', 'w4')}
        owner = Client(base, actors['owner']['token'])
        status, created = owner.post('/v1/relay/teams',
                                     {'title': '三席门禁演示', 'goal': '一个短码加入多名执行端'})
        check(status == 201, '创建团队会话')
        sid = created['session']['id']
        public_id = created['session']['public_id']

        status, opened = owner.post('/v1/relay/teams/' + sid + '/gate',
                                    {'role': 'worker', 'max_joins': 3, 'note': '本轮执行端'})
        check(status == 201, '创建者开一道 worker 门禁')
        gate = opened['gate']
        code = gate['code']
        check(len(code) == 6 and '\n' not in gate['join_hint']
              and 'diaodu_relay_team_gate_join' in gate['join_hint'],
              '返回 6 位短码和一行 MCP 加入说明')
        print('\n交给执行端的一行说明：')
        print('  ' + gate['join_hint'])

        seats = {}
        for name in ('w1', 'w2', 'w3'):
            client = Client(base, actors[name]['token'])
            status, joined = client.post('/v1/relay/teams/gate/join',
                                         {'gate_code': code, 'environment': ENVIRONMENTS[name]})
            check(status == 200 and joined['session']['role'] == 'worker',
                  name + ' 用同一个短码加入')
            check(code not in json.dumps(joined), name + ' 的加入响应不含门禁码')
            seats[name] = joined['session']['seat']
        check(sorted(seats.values()) == [1, 2, 3], '三名执行端分到 1、2、3 号席')

        extra = Client(base, actors['w4']['token'])
        status, rejected = extra.post('/v1/relay/teams/gate/join',
                                      {'gate_code': code, 'environment': ENVIRONMENTS['w4']})
        check(status == 403 and '名额' in rejected.get('error', ''),
              '第四个执行端被拒绝（名额已用尽）')

        status, ledger = owner.get('/v1/relay/teams/' + sid + '/gate?role=worker')
        check(status == 200 and ledger['gate']['remaining'] == 0
              and 'code' not in ledger['gate'] and code not in json.dumps(ledger),
              '创建者查看状态时看到剩余名额，响应里没有明文码')
        check(owner.get('/v1/relay/teams/' + sid + '/gate')[0] == 200, '创建者可以列出门禁')
        worker_view = Client(base, actors['w1']['token']).get('/v1/relay/teams/' + sid + '/gate')
        check(worker_view[0] == 403 and code not in json.dumps(worker_view[1]),
              '已加入的执行端不能读回门禁码')

        status, public = Client(base).get(
            '/v1/relay/public/conversations/' + public_id + '/events?wait=0')
        public_text = json.dumps(public)
        check(status == 200 and code not in public_text and 'DemoOS' not in public_text
              and 'environment' not in public_text,
              '公开目录不含门禁码和环境')

        status, closed = owner.post('/v1/relay/teams/' + sid + '/gate/close', {})
        check(status == 200 and closed['gate']['status'] == 'closed', '创建者提前关闭门禁')
        status, blocked = extra.post('/v1/relay/teams/gate/join',
                                     {'gate_code': code, 'environment': ENVIRONMENTS['w4']})
        check(status == 403 and '关闭' in blocked.get('error', ''), '关闭后短码不能再加入')

        status, invite = owner.post('/v1/relay/teams/' + sid + '/invites',
                                    {'role': 'mentor', 'note': '指导席仍用一次性邀请'})
        check(status == 201 and len(invite['invite']['code']) > 20, '一次性邀请仍然可以生成')
        mentor = Client(base, actors['mentor']['token'])
        status, joined = mentor.post('/v1/relay/teams/join', {'code': invite['invite']['code']})
        check(status == 200 and joined['session']['seat'] == 0, 'mentor 仍用一次性邀请加入')
        status, reused = mentor.post('/v1/relay/teams/join', {'code': invite['invite']['code']})
        check(status == 403, '同一条一次性邀请不能用第二次')

        with sqlite3.connect(library.teams.path) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(team_gates)')]
            stored = db.execute('SELECT code_hash FROM team_gates').fetchone()[0]
            payloads = [row[0] for row in db.execute('SELECT payload FROM team_events')]
        check('code' not in columns
              and stored == hashlib.sha256(code.encode()).hexdigest()
              and all(code not in item for item in payloads),
              '库里只存门禁码哈希，事件正文没有明文码')

        elapsed = time.monotonic() - started
        print('\n演示通过：一道短码加入 3 名执行端，第 4 个被拒绝，关闭后失效，'
              '一次性邀请仍可用。耗时 %.2fs' % elapsed)
        return 0
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        temporary.cleanup()


if __name__ == '__main__':
    raise SystemExit(main())
