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
import tempfile
import uuid
from urllib.error import HTTPError
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

    def request(self, path, body=None, timeout=35):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('需要API相对路径')
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
                   'User-Agent': 'diaodu-relay-client/1'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
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
            raise RelayError(error.code, str(message)) from None

    def connect(self, alias, agent_type='通用Agent'):
        if self.token:
            return dict(self.request('/me'), reused=True)
        result = self.request('/connect', {'alias': alias, 'agent_type': agent_type, 'member_kind': 'agent'})
        self.token = result.pop('token')
        result.pop('csrf', None)
        return result

    def create(self, title, goal):
        return self.request('/relay/sessions', {'title': title, 'goal': goal})

    def join(self, code, role, reported_environment=None):
        if role not in ('mentor', 'worker'):
            raise ValueError('role须为mentor或worker')
        body = {'code': code, 'role': role}
        if role == 'worker':
            body['environment'] = reported_environment if reported_environment is not None else environment()
        return self.request('/relay/join', body)

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
            after = args.after if args.after is not None else config.get('cursors', {}).get(args.session, 0)
            result = client.events(args.session, after, args.wait)
        elif args.command == 'send':
            text = args.text_file.read_text() if args.text_file else args.text
            result = client.send(args.session, args.kind, text, args.request_id, args.reply_to)
        elif args.command == 'ack':
            result = client.ack(args.session, args.seq)
            config.setdefault('cursors', {})[args.session] = result['acknowledged']
            save_state(args.state_file, saved)
        elif args.command == 'heartbeat':
            result = client.heartbeat(args.session, args.report_environment)
        else:
            result = client.state(args.session, args.status)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (RelayError, ValueError, OSError) as error:
        parser.exit(1, '请求失败：' + str(error) + '\n')


if __name__ == '__main__':
    main()
