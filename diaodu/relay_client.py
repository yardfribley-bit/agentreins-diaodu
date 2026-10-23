#!/usr/bin/env python3
"""Portable HTTP relay client. No model hosting or shell-command execution."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_URL = 'https://www.chuhaijian.com/task/v1'


def environment():
    """Only OS capabilities: never enumerate host/user names or environment secrets."""
    shells = [name for name in ('pwsh', 'powershell', 'cmd', 'bash', 'zsh', 'sh')
              if shutil.which(name)]
    system = platform.system() or 'Unknown'
    version = platform.release() or 'Unknown'
    if system == 'Darwin':
        system, version = 'macOS', platform.mac_ver()[0] or version
    elif system == 'Windows':
        version = ' / '.join(filter(None, platform.win32_ver()[:2])) or version
    elif system == 'Linux':
        try:
            distro = platform.freedesktop_os_release().get('PRETTY_NAME', '')
            version = (distro + '; kernel ' + version) if distro else version
        except (OSError, AttributeError):
            pass
    return {'os_name': system, 'os_version': version[:160],
            'architecture': platform.machine() or 'Unknown', 'shells': shells,
            'python_version': platform.python_version()}


class RelayError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None  # Do not forward a Bearer credential to another location.


class RelayClient:
    def __init__(self, url=DEFAULT_URL, token=None):
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('API URL不能含凭据、查询参数或片段')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
            raise ValueError('远程接入必须使用HTTPS；HTTP仅允许本机测试')
        if not parsed.hostname:
            raise ValueError('API URL缺少主机')
        self.url = url.rstrip('/')
        self.token = token if token is not None else os.environ.get('DIAODU_TOKEN', '')
        self.opener = build_opener(NoRedirect())
        self.max_retries = 3
        self.retry_base = 0.5
        self._sleep = time.sleep

    def _retry_delay(self, attempt, headers=None):
        if headers is not None:
            value = headers.get('Retry-After')
            if value:
                try:
                    return min(max(float(value), 0.0), 10.0)
                except (TypeError, ValueError):
                    pass
        return min(self.retry_base * (2 ** attempt), 10.0)

    def request(self, path, body=None, timeout=35, idempotent=True):
        """Retry 429 and transient failures; ambiguous writes opt out via idempotent=False."""
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('需要API相对路径')
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        attempt = 0
        while True:
            headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
                       'User-Agent': 'diaodu-relay-client/1'}
            if self.token:
                headers['Authorization'] = 'Bearer ' + self.token
            request = Request(self.url + path, data=data, headers=headers)
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    return json.load(response)
            except HTTPError as error:
                with error:
                    try:
                        message = json.load(error).get('error', '请求失败')
                    except (ValueError, AttributeError):
                        message = 'HTTP ' + str(error.code)
                    retryable = error.code == 429 or (
                        idempotent and error.code in (500, 502, 503, 504))
                    retry_headers = error.headers
                if retryable and attempt < self.max_retries:
                    self._sleep(self._retry_delay(attempt, retry_headers))
                    attempt += 1
                    continue
                raise RelayError(error.code, str(message)) from None
            except (URLError, TimeoutError, ConnectionError) as error:
                if idempotent and attempt < self.max_retries:
                    self._sleep(self._retry_delay(attempt))
                    attempt += 1
                    continue
                reason = getattr(error, 'reason', error)
                raise RelayError(0, '网络连接失败：' + str(reason)) from None

    def connect(self, alias, agent_type='通用Agent'):
        if self.token:
            return dict(self.request('/me'), reused=True)
        result = self.request('/connect', {'alias': alias, 'agent_type': agent_type, 'member_kind': 'agent'},
                              idempotent=False)
        self.token = result.pop('token')
        result.pop('csrf', None)
        return result

    def create(self, title, goal):
        return self.request('/relay/sessions', {'title': title, 'goal': goal}, idempotent=False)

    def join(self, code, role, reported_environment=None):
        if role not in ('mentor', 'worker'):
            raise ValueError('role须为mentor或worker')
        body = {'code': code, 'role': role}
        if role == 'worker':
            body['environment'] = reported_environment if reported_environment is not None else environment()
        return self.request('/relay/join', body, idempotent=False)

    def list(self):
        return self.request('/relay/sessions')

    @staticmethod
    def path(sid):
        return '/relay/sessions/' + quote(sid, safe='')

    def get(self, sid):
        return self.request(self.path(sid))

    def events(self, sid, after=0, wait=20):
        return self.request(self.path(sid) + '/events?' + urlencode({'after': after, 'wait': wait}), timeout=35)

    def send(self, sid, kind, text, request_id=None, reply_to=None):
        """Keep one request_id for a logical message so transport retries stay idempotent."""
        body = {'kind': kind, 'text': text, 'request_id': request_id or str(uuid.uuid4())}
        if reply_to is not None:
            body['reply_to'] = reply_to
        return self.request(self.path(sid) + '/messages', body)

    def ack(self, sid, seq):
        return self.request(self.path(sid) + '/ack', {'seq': seq})

    def heartbeat(self, sid, report_environment=False):
        return self.request(self.path(sid) + '/heartbeat', {'environment': environment()} if report_environment else {})

    def state(self, sid, status):
        return self.request(self.path(sid) + '/state', {'status': status})


def load_state(path):
    if path.is_symlink():
        raise ValueError('凭据文件不能是符号链接')
    if not path.exists():
        return {}
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise ValueError('凭据文件权限过宽，请先设置为0600')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('凭据文件格式不正确')
    return value


def _cursor_value(config, field, sid, default=None):
    cursors = config.get(field)
    if not isinstance(cursors, dict):
        return default
    value = cursors.get(sid)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def acked_cursor(config, sid):
    """Last seq the user acknowledged; unacked events replay, so this is the safe resume point."""
    return _cursor_value(config, 'cursors', sid, 0)


def read_cursor(config, sid):
    """Last seq watch already printed; None for state files written by older versions."""
    return _cursor_value(config, 'read_cursors', sid, None)


def remember_read_cursor(config, sid, cursor):
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        return
    config.setdefault('read_cursors', {})[sid] = cursor


def watch_loop(client, sid, cursor, wait, emit=print, on_cursor=None):
    """Print each batch as one JSON line per event until Ctrl-C; returns the last cursor."""
    try:
        while True:
            batch = client.events(sid, cursor, wait)
            for event in batch.get('events') or []:
                emit(json.dumps(event, ensure_ascii=False))
            next_cursor = batch.get('next_cursor', cursor)
            if isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor < cursor:
                raise RelayError(0, '实时接口没有推进游标，已停止 watch')
            cursor = next_cursor
            if on_cursor is not None:
                on_cursor(cursor)
    except KeyboardInterrupt:
        return cursor


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('凭据文件不能是符号链接')
    fd, name = tempfile.mkstemp(prefix='.relay-', dir=path.parent)
    try:
        os.chmod(name, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(state, output, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _wait_seconds(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('wait 须为 1 到 20 的整数')
    if not 1 <= number <= 20:
        raise argparse.ArgumentTypeError('wait 须为 1 到 20 的整数')
    return number


def main():
    parser = argparse.ArgumentParser(description='实时协作：指导端与执行端的通用HTTP客户端')
    parser.add_argument('--url', default=DEFAULT_URL, help='以/v1结尾的API地址')
    parser.add_argument('--state-file', type=Path, default=Path.home() / '.diaodu' / 'relay-client.json')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('environment')
    commands.add_parser('list')
    connect = commands.add_parser('connect')
    connect.add_argument('--alias', required=True)
    connect.add_argument('--type', default='通用Agent')
    create = commands.add_parser('create')
    create.add_argument('--title', required=True)
    create.add_argument('--goal', required=True)
    join = commands.add_parser('join')
    join.add_argument('--role', choices=('mentor', 'worker'), required=True)
    join.add_argument('--alias', default='通用Agent', help='首次接入时用于标识此Agent')
    join.add_argument('--type', default='通用Agent', help='首次接入时的Agent类型')
    join.add_argument('--code-file', type=Path, help='或设置DIAODU_PAIR_CODE；交互终端可安全输入')
    watch = commands.add_parser('watch', help='常驻拉取并逐行打印事件，Ctrl-C 退出')
    watch.add_argument('session')
    watch.add_argument('--after', type=int, default=None, help='默认续读上次 watch 的展示位置')
    watch.add_argument('--wait', type=_wait_seconds, default=20, help='每次长轮询等待秒数，1 到 20')
    for name in ('get', 'events', 'send', 'ack', 'heartbeat', 'state'):
        sub = commands.add_parser(name)
        sub.add_argument('session')
        if name == 'events':
            sub.add_argument('--after', type=int, default=None)
            sub.add_argument('--wait', type=int, default=20)
        elif name == 'send':
            sub.add_argument('--kind', choices=('instruction', 'progress', 'result', 'question', 'note'), required=True)
            texts = sub.add_mutually_exclusive_group(required=True)
            texts.add_argument('--text')
            texts.add_argument('--text-file', type=Path)
            sub.add_argument('--request-id', required=True)
            sub.add_argument('--reply-to', type=int)
        elif name == 'ack':
            sub.add_argument('seq', type=int)
        elif name == 'heartbeat':
            sub.add_argument('--report-environment', action='store_true', help='仅执行端使用')
        elif name == 'state':
            sub.add_argument('status', choices=('active', 'paused', 'closed'))
    args = parser.parse_args()
    try:
        if args.command == 'environment':
            print(json.dumps(environment(), ensure_ascii=False, indent=2))
            return
        saved = load_state(args.state_file)
        key = args.url.rstrip('/')
        config = saved.setdefault(key, {})
        client = RelayClient(key, os.environ.get('DIAODU_TOKEN') or config.get('token', ''))

        def persist():
            try:
                save_state(args.state_file, saved)
            except OSError as error:
                print('游标未能写入状态文件：' + str(error), file=sys.stderr)

        if args.command not in ('connect', 'join') and not client.token:
            parser.error('先connect或通过DIAODU_TOKEN提供凭据')
        if args.command == 'connect':
            result = client.connect(args.alias, args.type)
            config['token'] = client.token
            save_state(args.state_file, saved)
            result['credential_saved'] = str(args.state_file)
        elif args.command == 'list':
            result = client.list()
        elif args.command == 'create':
            result = client.create(args.title, args.goal)
        elif args.command == 'join':
            code = args.code_file.read_text().strip() if args.code_file else os.environ.get('DIAODU_PAIR_CODE') or getpass.getpass('单次配对码：')
            if not client.token:
                # A Topic code is enough to bootstrap this Agent. Save its own
                # platform identity before joining so a typo can be retried.
                client.connect(args.alias, args.type)
                config['token'] = client.token
                save_state(args.state_file, saved)
            result = client.join(code, args.role)
        elif args.command == 'get':
            result = client.get(args.session)
        elif args.command == 'events':
            # Unacked events replay on purpose; --after wins when the caller keeps its own cursor.
            after = args.after if args.after is not None else acked_cursor(config, args.session)
            result = client.events(args.session, after, args.wait)
            remember_read_cursor(config, args.session, result.get('next_cursor', after))
            persist()
        elif args.command == 'watch':
            resume = read_cursor(config, args.session)
            after = args.after if args.after is not None else (
                resume if resume is not None else acked_cursor(config, args.session))
            print('watch 已启动：' + args.session + '，从 #' + str(after) + ' 开始，Ctrl-C 退出',
                  file=sys.stderr)

            def remember(cursor):
                remember_read_cursor(config, args.session, cursor)
                persist()

            stopped = watch_loop(client, args.session, after, args.wait, on_cursor=remember)
            print('watch 已停止，展示到 #' + str(stopped), file=sys.stderr)
            return
        elif args.command == 'send':
            text = args.text_file.read_text() if args.text_file else args.text
            result = client.send(args.session, args.kind, text, args.request_id, args.reply_to)
        elif args.command == 'ack':
            result = client.ack(args.session, args.seq)
            config.setdefault('cursors', {})[args.session] = result['acknowledged']
            persist()
        elif args.command == 'heartbeat':
            result = client.heartbeat(args.session, args.report_environment)
        else:
            result = client.state(args.session, args.status)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (RelayError, ValueError, OSError) as error:
        parser.exit(1, '请求失败：' + str(error) + '\n')


if __name__ == '__main__':
    main()
