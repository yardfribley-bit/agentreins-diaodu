"""Reverse proxy from Hub task auth to the local design MCP.

Callers who can already use /v1 authenticate with the same session cookie or
Hub bearer. The design service bearer (DESIGN_MCP_TOKEN) is attached only on
the upstream request and is never returned to the client.

Streamable HTTP responses are written through as they arrive. Nginx, which
already forwards /task/, is asked not to buffer them via X-Accel-Buffering.
"""
import os
import socket
import sys
import traceback
from urllib.parse import urlparse, urlunparse

import http.client

from diaodu.http_util import discard_body
from diaodu.identity import AuthError

PREFIX = '/v1/mcp/design'
HEALTH_PATH = PREFIX + '/health'
DEFAULT_UPSTREAM = 'http://127.0.0.1:18790/mcp/design/'
DEFAULT_TIMEOUT = 600.0
MAX_BODY = 100 * 1024 * 1024
_CHUNK = 64 * 1024

_HOP = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade', 'host',
}
_REQUEST_PASS = {
    'content-type', 'accept', 'accept-language', 'mcp-protocol-version',
    'last-event-id',
}


def is_design_path(path):
    return path == PREFIX or path.startswith(PREFIX + '/')


def design_token():
    token = os.environ.get('DESIGN_MCP_TOKEN', '').strip()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()
    if any(char in token for char in '\r\n\x00'):
        return ''
    return token


def upstream_base():
    raw = os.environ.get('DESIGN_MCP_UPSTREAM', '').strip() or DEFAULT_UPSTREAM
    parsed = urlparse(raw)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('DESIGN_MCP_UPSTREAM 无效')
    if parsed.username or parsed.password:
        raise ValueError('DESIGN_MCP_UPSTREAM 不能包含用户信息')
    return raw


def timeout_seconds():
    raw = os.environ.get('DESIGN_MCP_TIMEOUT', '').strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    if not 1 <= value <= 3600:
        return DEFAULT_TIMEOUT
    return value


def health_payload():
    try:
        upstream_base()
        valid = True
    except ValueError:
        valid = False
    return {
        'ok': True,
        'service': 'design-mcp',
        'configured': valid and bool(design_token()),
    }


def handle(handler, method):
    path = urlparse(handler.path).path
    if path.rstrip('/') == HEALTH_PATH:
        if method in ('GET', 'HEAD'):
            handler._json(200, health_payload())
            return
        _discard(handler)
        handler._json(405, {'error': '健康检查请使用 GET'}, {'Allow': 'GET'})
        return
    proxy(handler, method)


def proxy(handler, method):
    """Authenticate as a Hub task caller, then stream the MCP request upstream."""
    try:
        handler._actor(mutation=method in ('POST', 'DELETE'))
    except AuthError:
        _discard(handler)
        raise
    try:
        length = _declared_length(handler)
    except ValueError:
        _discard(handler)
        raise
    if not _configured():
        _discard(handler)
        handler._json(503, {'error': '设计服务未配置'})
        return
    if length > MAX_BODY:
        _discard(handler)
        handler.close_connection = True
        raise ValueError('设计 MCP 请求不能超过 100MB')
    try:
        parsed = urlparse(handler.path)
        target = _target(parsed.path, parsed.query)
    except ValueError:
        _discard(handler)
        raise
    conn = None
    started = False
    try:
        conn = _connect(target)
        _send_request(conn, handler, method, target, length)
        upstream = conn.getresponse()
        started = True
        _relay(handler, upstream, method)
    except ValueError:
        raise
    except Exception:
        if started:
            handler.close_connection = True
            return
        traceback.print_exc(file=sys.stderr)
        handler._json(502, {'error': '设计服务暂时不可用'})
    finally:
        if conn is not None:
            conn.close()


def _configured():
    try:
        upstream_base()
    except ValueError:
        return False
    return bool(design_token())


def _declared_length(handler):
    if handler.headers.get('Transfer-Encoding'):
        raise ValueError('不支持分块上传，请使用 Content-Length')
    raw = handler.headers.get('Content-Length')
    if raw is None or raw == '':
        return 0
    try:
        length = int(raw)
    except ValueError:
        raise ValueError('Content-Length 不合法')
    if length < 0:
        raise ValueError('Content-Length 不合法')
    return length


def _discard(handler):
    try:
        length = int(handler.headers.get('Content-Length', '0') or 0)
    except ValueError:
        handler.close_connection = True
        return
    if length > 0 and not discard_body(handler.rfile, length):
        handler.close_connection = True


def _target(path, query):
    base = urlparse(upstream_base())
    suffix = path[len(PREFIX):]
    if suffix not in ('', '/'):
        if not suffix.startswith('/') or any(part == '..' for part in suffix.split('/')):
            raise ValueError('路径不合法')
        target_path = (base.path or '/').rstrip('/') + suffix
    else:
        # Both /v1/mcp/design and /v1/mcp/design/ hit the configured endpoint,
        # including the trailing slash the design service expects.
        target_path = base.path or '/'
    target_query = base.query
    if query:
        target_query = target_query + '&' + query if target_query else query
    return urlunparse((base.scheme, base.netloc, target_path, '', target_query, ''))


def _connect(url):
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    timeout = timeout_seconds()
    if parsed.scheme == 'https':
        return http.client.HTTPSConnection(parsed.hostname, port, timeout=timeout)
    return http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)


def _send_request(conn, handler, method, url, length):
    parsed = urlparse(url)
    target = parsed.path or '/'
    if parsed.query:
        target += '?' + parsed.query
    conn.putrequest(method, target, skip_accept_encoding=True)
    conn.putheader('Authorization', 'Bearer ' + design_token())
    conn.putheader('Content-Length', str(length))
    for key, value in _request_headers(handler):
        conn.putheader(key, value)
    conn.endheaders()
    _nodelay(conn.sock)
    remaining = length
    while remaining:
        chunk = handler.rfile.read(min(_CHUNK, remaining))
        if not chunk:
            raise ValueError('上传未完成')
        conn.send(chunk)
        remaining -= len(chunk)


def _request_headers(handler):
    token = design_token()
    for key in handler.headers:
        lowered = key.lower()
        if lowered in _HOP or lowered in ('authorization', 'cookie', 'content-length', 'transfer-encoding', 'x-csrf-token'):
            continue
        if lowered not in _REQUEST_PASS and not lowered.startswith('mcp-'):
            continue
        value = handler.headers.get(key)
        if not value or '\r' in value or '\n' in value:
            continue
        if token and token in value:
            continue
        yield key, value


def _relay(handler, upstream, method):
    length = upstream.length
    token = design_token()
    handler.send_response(upstream.status, upstream.reason)
    for key, value in upstream.getheaders():
        if not _pass_response(key, value, token):
            continue
        handler.send_header(key, value)
    # Ask Nginx (proxy_pass of /task/) not to buffer SSE or long tool calls.
    handler.send_header('X-Accel-Buffering', 'no')
    handler.send_header('Cache-Control', 'no-cache, no-store, no-transform')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    if length is None:
        handler.close_connection = True
        handler.send_header('Connection', 'close')
    else:
        handler.send_header('Content-Length', str(length))
    handler.end_headers()
    _nodelay(getattr(handler, 'connection', None))
    if method == 'HEAD' or getattr(handler, '_head_only', False) or length == 0:
        return
    try:
        while True:
            chunk = upstream.read1(_CHUNK)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True


def _pass_response(name, value, token):
    lowered = name.lower()
    if lowered in _HOP or lowered in {
        'set-cookie', 'authorization', 'www-authenticate', 'proxy-authenticate',
        'content-length', 'server', 'date', 'cache-control',
    }:
        return False
    if '\r' in value or '\n' in value:
        return False
    if token and token in value:
        return False
    return lowered in ('content-type', 'pragma', 'location', 'content-encoding', 'vary') or lowered.startswith('mcp-') or lowered == 'last-event-id'


def _nodelay(sock):
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
