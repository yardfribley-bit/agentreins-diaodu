"""REST routes for the one-to-many relay teams (separate from /v1/relay/sessions)."""
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

from diaodu.identity import AuthError
from diaodu.relay_http import _base_url, _rate_limit


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
        if set(query) - {'after', 'wait'} or any(len(values) != 1 for values in query.values()):
            raise ValueError('仅支持 after 与 wait 查询参数')
        after = int(query.get('after', ['0'])[0])
        wait = 0 if getattr(handler, '_head_only', False) else int(query.get('wait', ['0'])[0])
        result = teams.events(actor, parts[3], after=after, wait=wait)
        # Membership and credentials can be revoked during a long poll.
        actor = handler._actor()
        teams.get(actor, parts[3])
    elif len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'invites':
        result = teams.invites(actor, parts[3])
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
        raise ValueError('单个文件不能超过 100 MB')
    name = parse_qs(urlparse(handler.path).query).get('name', [''])[0]
    handler._json(201, teams.receive(actor, sid, name, handler.rfile, length))


def post(handler, library, parts):
    if len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'files':
        _upload(handler, library, parts)
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
    if len(parts) == 5 and parts[:3] == ['v1', 'relay', 'teams']:
        if parts[4] == 'invites':
            handler._json(201, teams.invite(actor, parts[3], body))
            return
        action = {'messages': teams.send, 'ack': teams.ack, 'state': teams.state,
                  'revoke': teams.revoke, 'heartbeat': teams.heartbeat,
                  'public': teams.set_public}.get(parts[4])
        if action is None:
            handler._json(404, {'error': '没有这个团队协作动作'})
            return
        handler._json(200, action(actor, parts[3], body))
        return
    if len(parts) == 6 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'invites' \
            and parts[5] == 'reset':
        handler._json(200, teams.reset_invites(actor, parts[3]))
        return
    if len(parts) == 7 and parts[:3] == ['v1', 'relay', 'teams'] and parts[4] == 'invites' \
            and parts[6] == 'revoke':
        handler._json(200, teams.revoke_invite(actor, parts[3], parts[5]))
        return
    handler._json(404, {'error': '没有这个团队协作接口'})
