"""Private relay REST routes; the shared handler owns authentication and CSRF."""
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def get(handler, library, parsed):
    parts = parsed.path.strip('/').split('/')
    if parts == ['v1', 'relay', 'guide.md']:
        handler._file(Path(__file__).parent.parent / 'docs' / 'relay.md')
        return
    if parts == ['v1', 'relay', 'client.py']:
        handler._file(Path(__file__).parent / 'relay_client.py')
        return
    actor = handler._actor()
    relay = library.relay
    if len(parts) == 6 and parts[:3] == ['v1', 'relay', 'sessions'] and parts[4] == 'files':
        item, path = relay.locate_file(actor, parts[3], parts[5])
        handler._stream_file(item, path, content_type=item.get('type'), inline=item.get('inline', False))
        return
    if parts == ['v1', 'relay', 'sessions']:
        result = relay.list_sessions(actor)
    elif len(parts) == 4 and parts[:3] == ['v1', 'relay', 'sessions']:
        result = relay.get(actor, parts[3])
    elif len(parts) == 5 and parts[:3] == ['v1', 'relay', 'sessions'] and parts[4] == 'events':
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) - {'after', 'wait'} or any(len(values) != 1 for values in query.values()):
            raise ValueError('仅支持 after 与 wait 查询参数')
        after = int(query.get('after', ['0'])[0])
        wait = 0 if getattr(handler, '_head_only', False) else int(query.get('wait', ['0'])[0])
        result = relay.events(actor, parts[3], after=after, wait=wait)
        # Credentials and membership can be revoked during a long poll.
        actor = handler._actor()
        relay.get(actor, parts[3])
    else:
        handler._json(404, {'error': '没有这个实时协作接口'})
        return
    try:
        handler._json(200, result)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _upload(handler, library, parts):
    """Raw-byte upload; streaming to disk and hashing happen before the store lock."""
    actor = handler._actor(mutation=True)
    relay = library.relay
    sid = parts[3]
    length = int(handler.headers.get('Content-Length', '0'))
    if length <= 0:
        raise ValueError('上传内容不能为空')
    if length > relay.MAX_FILE_BYTES:
        raise ValueError('单个文件不能超过 100 MB')
    name = parse_qs(urlparse(handler.path).query).get('name', [''])[0]
    result = relay.receive(actor, sid, name, handler.rfile, length)
    handler._json(201, result)


def post(handler, library, parts):
    if len(parts) == 5 and parts[:3] == ['v1', 'relay', 'sessions'] and parts[4] == 'files':
        _upload(handler, library, parts)
        return
    actor = handler._actor(mutation=True)
    body = handler._body(limit=65536)
    relay = library.relay
    status = 200
    if parts == ['v1', 'relay', 'sessions']:
        result = relay.create(actor, body)
        status = 201
    elif parts == ['v1', 'relay', 'join']:
        result = relay.join(actor, body)
    elif len(parts) == 5 and parts[:3] == ['v1', 'relay', 'sessions']:
        action = {'messages': relay.send, 'ack': relay.ack, 'heartbeat': relay.heartbeat,
                  'state': relay.state, 'invites': relay.invite, 'revoke': relay.revoke,
                  'topic-code': relay.rotate_topic_code}.get(parts[4])
        if action is None:
            handler._json(404, {'error': '没有这个实时协作动作'})
            return
        result = action(actor, parts[3], body)
    else:
        handler._json(404, {'error': '没有这个实时协作接口'})
        return
    handler._json(status, result)
