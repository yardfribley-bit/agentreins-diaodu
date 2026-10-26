"""REST routes for the one-to-many relay teams (separate from /v1/relay/sessions)."""
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

from diaodu.identity import AuthError
from diaodu.relay_http import _base_url, _client_ip, _rate_limit, client_abandoned


def _invite_info(handler, library, code):
    info = library.teams.invite_info(unquote(code))
    base = _base_url(handler, library)
    info.update(mcp_url=base + '/v1/mcp', guide_url=base + '/v1/relay/guide.md',
                join_url=base + '/relay/join/' + quote(code, safe=''))
    return info


def get(handler, library, parsed):
    parts = parsed.path.strip('/').split('/')
    teams = library.teams
    if len(parts) == 5 and parts[:4] == ['v1', 'relay', 'teams', 'invites']:
        _rate_limit(handler)
        handler._json(200, _invite_info(handler, library, parts[4]))
        return
    if len(parts) == 6 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'files':
        query = parse_qs(parsed.query)
        if 'sig' in query or 'exp' in query:
            try:
                expires = int(query.get('exp', [''])[0])
            except ValueError:
                raise AuthError('签名链接不合法', 403)
            if expires < time.time():
                raise AuthError('签名链接已过期，请重新获取', 403)
            if not teams.verify_signature(parts[3], parts[5], expires, query.get('sig', [''])[0]):
                raise AuthError('签名链接不合法', 403)
            item, path = teams.locate_file_unsigned(parts[3], parts[5])
        else:
            item, path = teams.locate_file(handler._actor(), parts[3], parts[5])
        handler._stream_file(item, path, content_type=item.get('type'), inline=item.get('inline', False))
        return
    actor = handler._actor()
    if parts == ['v1', 'relay', 'teams']:
        result = teams.list_sessions(actor)
    elif len(parts) == 4 and parts[:3] == ['v1', 'relay', 'teams']:
        result = teams.get(actor, parts[3])
    elif len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'events':
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) - {'after', 'wait', 'stream', 'view'} or any(len(values) != 1 for values in query.values()):
            raise ValueError('仅支持 after、wait、stream 与 view 查询参数')
        after = int(query.get('after', ['0'])[0])
        wait = 0 if getattr(handler, '_head_only', False) else int(query.get('wait', ['0'])[0])
        raw_view = query.get('view', ['1'])[0]
        if raw_view not in ('0', '1'):
            raise ValueError('view须为 0 或 1')
        result = teams.events(actor, parts[3], after=after, wait=wait,
                              stream=query.get('stream', ['work'])[0],
                              view=int(raw_view),
                              abandon=lambda: client_abandoned(handler))
        # Membership and credentials can be revoked during a long poll.
        actor = handler._actor()
        teams.get(actor, parts[3])
    elif len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'invites':
        result = teams.invites(actor, parts[3])
    elif len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'leader':
        result = teams.who_is_leader(actor, parts[3])
    elif len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'online':
        result = teams.list_online(actor, parts[3])
    elif len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'report':
        result = teams.read_report(actor, parts[3])
    elif len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'tasks':
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) - {'status'} or any(len(values) != 1 for values in query.values()):
            raise ValueError('仅支持 status 查询参数')
        result = teams.list_tasks(actor, parts[3], query.get('status', [None])[0])
    elif len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'gate':
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) - {'role'} or any(len(values) != 1 for values in query.values()):
            raise ValueError('仅支持 role 查询参数')
        result = teams.gate_status(actor, parts[3], query.get('role', [None])[0])
    else:
        handler._json(404, {'error': '没有这个团队协作接口'})
        return
    try:
        handler._json(200, result)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _upload(handler, library, parts):
    actor = handler._actor(mutation=True)
    teams = library.teams
    sid = parts[3]
    length = int(handler.headers.get('Content-Length', '0'))
    if length <= 0:
        raise ValueError('上传内容不能为空')
    if length > teams.MAX_FILE_BYTES:
        handler._reject_oversized(length, '单个文件不能超过 100 MB')
    name = parse_qs(urlparse(handler.path).query).get('name', [''])[0]
    handler._json(201, teams.receive(actor, sid, name, handler.rfile, length))


def post(handler, library, parts):
    if len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'files':
        _upload(handler, library, parts)
        return
    if len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'report':
        # Reports are far larger than chat messages. 4MB covers 40 万汉字 and
        # the JSON wrapper; the store still enforces MAX_REPORT_CHARS.
        actor = handler._actor(mutation=True)
        body = handler._body(limit=4_000_000)
        handler._json(200, library.teams.submit_report(actor, parts[3], body))
        return
    actor = handler._actor(mutation=True)
    body = handler._body(limit=65536)
    teams = library.teams
    if parts == ['v1', 'relay', 'teams']:
        handler._json(201, teams.create(actor, body))
        return
    if parts == ['v1', 'relay', 'teams', 'join']:
        handler._json(200, teams.join(actor, body))
        return
    if parts == ['v1', 'relay', 'teams', 'gate', 'join']:
        handler._json(200, teams.join_gate(actor, body, client_ip=_client_ip(handler)))
        return
    if len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams']:
        if parts[4] == 'invites':
            handler._json(201, teams.invite(actor, parts[3], body))
            return
        if parts[4] == 'gate':
            handler._json(201, teams.open_gate(actor, parts[3], body))
            return
        if parts[4] == 'tasks':
            handler._json(201, teams.create_task(actor, parts[3], body))
            return
        if parts[4] == 'leader':
            handler._json(200, teams.appoint(actor, parts[3], body))
            return
        action = {'messages': teams.send, 'ack': teams.ack, 'state': teams.state,
                  'revoke': teams.revoke, 'heartbeat': teams.heartbeat,
                  'public': teams.set_public}.get(parts[4])
        if action is None:
            handler._json(404, {'error': '没有这个团队协作动作'})
            return
        handler._json(200, action(actor, parts[3], body))
        return
    if len(parts) == 6 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'election':
        if parts[5] == 'campaign':
            handler._json(200, teams.campaign(actor, parts[3]))
            return
        if parts[5] == 'heartbeat':
            handler._json(200, teams.leader_heartbeat(actor, parts[3]))
            return
        if parts[5] == 'vote':
            handler._json(200, teams.vote(actor, parts[3], body))
            return
        handler._json(404, {'error': '没有这个团队协作动作'})
        return
    if len(parts) == 7 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'tasks':
        task_id = parts[5]
        if parts[6] == 'claim':
            handler._json(200, teams.claim_task(actor, parts[3], task_id))
            return
        if parts[6] == 'assign':
            handler._json(200, teams.assign_task(actor, parts[3], task_id, body))
            return
        if parts[6] == 'update':
            handler._json(200, teams.update_task(actor, parts[3], task_id, body))
            return
        handler._json(404, {'error': '没有这个团队协作动作'})
        return
    if len(parts) == 6 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'invites' \
            and parts[5] == 'reset':
        handler._json(200, teams.reset_invites(actor, parts[3]))
        return
    if len(parts) == 6 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'gate' \
            and parts[5] == 'close':
        handler._json(200, teams.close_gate(actor, parts[3], body))
        return
    if len(parts) == 6 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'gate' \
            and parts[5] == 'rotate':
        handler._json(200, teams.rotate_gate(actor, parts[3], body))
        return
    if len(parts) == 6 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'gate' \
            and parts[5] == 'expand':
        handler._json(200, teams.expand_gate(actor, parts[3], body))
        return
    if len(parts) == 7 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'invites' \
            and parts[6] == 'revoke':
        handler._json(200, teams.revoke_invite(actor, parts[3], parts[5]))
        return
    handler._json(404, {'error': '没有这个团队协作接口'})
