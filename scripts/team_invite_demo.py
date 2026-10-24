#!/usr/bin/env python3
"""One mentor + three workers, each joining through a different one-time invite link.

Runs a throwaway local server (127.0.0.1, random port, temporary data directory),
prints the invite links, sends one broadcast instruction and one targeted at seat 2,
then proves that seats 1 and 3 cannot read the targeted instruction while seat 2 can
answer it. It also asserts that the anonymous invite endpoint never exposes the
workers' machine environment.

    python3 scripts/team_invite_demo.py
"""
import json
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

    def text(self, path):
        with urlopen(self.base + path, timeout=30) as response:
            return response.status, response.read().decode()


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
        print('AgentReins 团队邀请演示 · ' + base + '/relay/')
        actors = {name: identity.connect(name, '演示Agent')
                  for name in ('owner', 'mentor', 'w1', 'w2', 'w3')}
        owner = Client(base, actors['owner']['token'])
        status, created = owner.post('/v1/relay/teams',
                                     {'title': '三席并行演示', 'goal': '展示席位邀请、定向派活与回报汇总'})
        check(status == 201, '创建团队会话')
        sid = created['session']['id']

        codes = {}
        status, result = owner.post('/v1/relay/teams/' + sid + '/invites',
                                    {'role': 'mentor', 'note': '给指挥端'})
        check(status == 201, '生成指导端邀请')
        codes['mentor'] = result['invite']['code']
        for index, name in enumerate(('w1', 'w2', 'w3'), start=1):
            status, result = owner.post('/v1/relay/teams/' + sid + '/invites',
                                        {'role': 'worker', 'seat': index, 'note': '给 ' + name})
            check(status == 201, '生成 %d 号席邀请' % index)
            codes[name] = result['invite']['code']

        print('\n三条互不相同的执行端邀请链接：')
        for name in ('w1', 'w2', 'w3'):
            print('  %s → %s/relay/join/%s' % (name, base, codes[name]))
        join_doc_status, join_doc = Client(base).text('/relay/join/' + codes['w2'])
        check(join_doc_status == 200 and codes['w2'] in join_doc
              and 'diaodu_relay_team_join' in join_doc,
              '邀请链接返回 agent 可读的接入说明（含本次邀请码）')

        # Anonymous invite lookup must never expose the joined workers' environment.
        status, info = Client(base).get('/v1/relay/teams/invites/' + codes['w2'])
        check(status == 200, '匿名查询邀请信息')
        serialized = json.dumps(info)
        for leak in ('environment', 'os_name', 'DemoOS', 'demo-arch', 'demo-shell',
                     'python_version', 'last_seen', 'ack_seq', 'actor_key', 'tm_'):
            check(leak not in serialized, '邀请公开信息不含 ' + leak)
        check(all(set(worker) == {'role', 'seat', 'name', 'online'} for worker in info['workers']),
              '公开席位字段只有 role/seat/name/online')

        # Each worker joins with its own one-time link. Reusing a link is rejected.
        mentor = Client(base, actors['mentor']['token'])
        status, _ = mentor.post('/v1/relay/teams/join', {'code': codes['mentor']})
        check(status == 200, 'mentor 用第一条邀请加入')
        workers = {}
        for name in ('w1', 'w2', 'w3'):
            client = Client(base, actors[name]['token'])
            status, joined = client.post('/v1/relay/teams/join',
                                         {'code': codes[name], 'environment': ENVIRONMENTS[name]})
            check(status == 200 and joined['session']['seat'] in (1, 2, 3),
                  name + ' 用专属邀请加入并分配到席位')
            workers[name] = client
        reused = Client(base, actors['owner']['token']).post(
            '/v1/relay/teams/join', {'code': codes['w1'],
                                     'environment': ENVIRONMENTS['w1']})
        check(reused[0] == 403, '同一条邀请第二次使用被拒绝（403）')

        # Anonymous public invite lookup again after workers reported environments.
        status, info = Client(base).get('/v1/relay/teams/invites/' + codes['w2'])
        serialized = json.dumps(info)
        check('DemoOS' not in serialized and 'demo-arch' not in serialized,
              '加入后邀请公开信息仍不含环境')

        # Broadcast + targeted instruction from the mentor.
        status, broadcast = mentor.post('/v1/relay/teams/' + sid + '/messages',
                                        {'kind': 'instruction', 'text': '全体：汇报各自进度',
                                         'request_id': 'demo-broadcast'})
        check(status == 200, 'mentor 广播一条指令')
        broadcast_seq = broadcast['event']['seq']
        status, targeted = mentor.post('/v1/relay/teams/' + sid + '/messages',
                                       {'kind': 'instruction', 'text': '只给二号席的秘密任务',
                                        'to': 'worker:2', 'request_id': 'demo-target-2'})
        check(status == 200, 'mentor 定向二号席一条指令')
        targeted_seq = targeted['event']['seq']

        def visible_seqs(client):
            status, data = client.get('/v1/relay/teams/%s/events?after=0&wait=0' % sid)
            check(status == 200, '读取团队事件')
            return [event['seq'] for event in data['events']]

        w1_seqs, w2_seqs, w3_seqs = (visible_seqs(workers['w1']), visible_seqs(workers['w2']),
                                     visible_seqs(workers['w3']))
        check(broadcast_seq in w1_seqs and broadcast_seq in w2_seqs and broadcast_seq in w3_seqs,
              '广播指令对三个席都可见')
        check(targeted_seq not in w1_seqs, '一号席读不到给二号席的定向指令')
        check(targeted_seq not in w3_seqs, '三号席读不到给二号席的定向指令')
        check(targeted_seq in w2_seqs, '二号席能读到给自己的定向指令')

        status, result = workers['w2'].post(
            '/v1/relay/teams/' + sid + '/messages',
            {'kind': 'result', 'text': '二号席已完成秘密任务', 'reply_to': targeted_seq,
             'request_id': 'result-%d' % targeted_seq})
        check(status == 200, '二号席回报结果并挂回原指令')

        status, data = mentor.get('/v1/relay/teams/%s/events?after=0&wait=0' % sid)
        check(status == 200, '指挥端读取全部事件')
        replies = [event for event in data['events']
                   if event['kind'] == 'message' and event['message'].get('kind') == 'result']
        by_reply = {event['message']['reply_to']: event for event in replies}
        check(by_reply.get(targeted_seq, {}).get('sender', {}).get('seat') == 2,
              '指挥端按 reply_to 看到二号席的回报')

        print('\n按 reply_to 汇总（指挥端视角）：')
        seats = [event['sender']['seat'] for event in data['events']
                 if event['kind'] == 'message' and event['message'].get('kind') == 'result']
        for seq, label, targets in ((broadcast_seq, '全体指令', (1, 2, 3)),
                                    (targeted_seq, '定向二号席', (2,))):
            reported = sorted({event['sender']['seat'] for event in replies
                               if event['message'].get('reply_to') == seq})
            missing = [seat for seat in targets if seat not in reported]
            print('  #%d %s → 已回报 %s；未回报 %s'
                  % (seq, label, reported or '无', missing or '无'))
        check(seats == [2], '当前只有二号席回报；一、三号席仍显示未回报')

        # Team public watch reuses the same filtered endpoint as one-to-one.
        status, enabled = owner.post('/v1/relay/teams/' + sid + '/public', {'enabled': True})
        check(status == 200 and '/relay/watch/' in enabled['public_url'], '开启团队公开围观')
        token = enabled['public_url'].rsplit('/', 1)[-1]
        status, public = Client(base).get('/v1/relay/public/%s/events?after=0&wait=0' % token)
        check(status == 200, '匿名读取公开围观')
        public_text = json.dumps(public)
        for leak in ('environment', 'DemoOS', 'demo-arch', 'demo-shell', 'tm_',
                     'actor_key', 'sig=', 'download_url'):
            check(leak not in public_text, '公开围观不含 ' + leak)

        elapsed = time.monotonic() - started
        print('\n演示通过：1 mentor + 3 workers，三条独立邀请链接，广播/定向可见性正确，'
              '公开邀请与公开围观均不含环境信息。耗时 %.2fs' % elapsed)
        return 0
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        temporary.cleanup()


if __name__ == '__main__':
    raise SystemExit(main())
