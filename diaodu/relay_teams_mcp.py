"""MCP tools for the one-to-many relay teams."""
import base64

from diaodu.relay_mcp import ENV, ROLE, S, validate_actor

KIND = {'type': 'string', 'enum': ['instruction', 'progress', 'result', 'question', 'note']}
TOOLS = [
    ('relay_team_create', '创建团队会话（一个指挥端 + 多个执行端），返回团队配对码。', {'title': S, 'goal': S}, ['title', 'goal']),
    ('relay_team_join', '用团队配对码加入：mentor 占指导席，worker 自动分配最小空闲执行席（需真实 environment）。', {'code': S, 'role': ROLE, 'environment': ENV}, ['code']),
    ('relay_team_list', '列出本人参与的团队会话。', {}, []),
    ('relay_team_get', '读取团队会话快照：指导席、各执行席在线/确认进度。', {'session': S}, ['session']),
    ('relay_team_read', '按可见性读取团队事件；游标为全局 seq，允许空洞。', {'session': S, 'after': {'type': 'integer', 'minimum': 0}, 'wait': {'type': 'integer', 'minimum': 0, 'maximum': 20}}, ['session']),
    ('relay_team_send', '发送指令/进度/结果/问题/备注。mentor/owner 可用 to="worker:N" 定向；worker 只能默认或 to="all"。可带 attachments（≤20）。', {'session': S, 'kind': KIND, 'text': S, 'request_id': S, 'reply_to': {'type': 'integer', 'minimum': 1}, 'to': S, 'attachments': {'type': 'array', 'items': S, 'maxItems': 20}}, ['session', 'kind', 'text', 'request_id']),
    ('relay_team_ack', '确认已接收至指定全局 seq（不等于执行完成）。', {'session': S, 'seq': {'type': 'integer', 'minimum': 0}}, ['session', 'seq']),
    ('relay_team_heartbeat', '刷新在线状态；执行端可更新系统环境。', {'session': S, 'environment': ENV}, ['session']),
    ('relay_team_state', '仅创建者：暂停、继续或结束团队会话。', {'session': S, 'status': {'type': 'string', 'enum': ['active', 'paused', 'closed']}}, ['session', 'status']),
    ('relay_team_revoke', '仅创建者：撤销指定席位（worker 省略 seat 表示撤销全部执行席）。', {'session': S, 'role': ROLE, 'seat': {'type': 'integer', 'minimum': 0}}, ['session', 'role']),
    ('relay_team_invite', '仅创建者：生成一次性席位邀请码（默认24小时）；worker 可指定 seat 或留空表示任意空席。', {'session': S, 'role': ROLE, 'seat': {'type': 'integer', 'minimum': 0}, 'note': S}, ['session', 'role']),
    ('relay_team_invites', '仅创建者：查看邀请账本（席位、备注、状态、使用人、时间）。', {'session': S}, ['session']),
    ('relay_team_invite_revoke', '仅创建者：撤销一条未使用的邀请。', {'session': S, 'invite_id': S}, ['session', 'invite_id']),
    ('relay_team_invite_reset', '仅创建者：紧急止损，撤销本会话所有未使用邀请。', {'session': S}, ['session']),
    ('relay_team_gate_open', '仅创建者：开一道短码加入门禁。同一角色同时只能有一道未关闭且未过期的门禁；worker 的 max_joins 为 1 到席位上限，mentor 只能为 1。ttl_hours 默认 24，最长 168。', {'session': S, 'role': ROLE, 'max_joins': {'type': 'integer', 'minimum': 1, 'maximum': 8}, 'ttl_hours': {'type': 'number', 'minimum': 1, 'maximum': 168}, 'note': S}, ['session', 'role', 'max_joins']),
    ('relay_team_gate_join', '用短门禁码加入。worker 必须提交真实 environment；服务端分配下一个空闲席位，直到名额用尽或门禁关闭/过期。', {'gate_code': S, 'role': ROLE, 'environment': ENV}, ['gate_code']),
    ('relay_team_gate_get', '仅创建者：查看加入门禁状态（剩余名额与使用者）。不返回门禁码；省略 role 时返回 worker 与 mentor 各自最新一道。', {'session': S, 'role': ROLE}, ['session']),
    ('relay_team_gate_close', '仅创建者：关闭该角色当前门禁，旧码立即不能再加入。省略 role 时关闭 worker 门禁。', {'session': S, 'role': ROLE}, ['session']),
    ('relay_team_gate_rotate', '仅创建者：更换短门禁码，剩余名额和过期时间不变，旧码立即失效。省略 role 时更换 worker 门禁。', {'session': S, 'role': ROLE}, ['session']),
    ('relay_team_upload', '上传团队附件（base64，最大10MB），发送消息时在 attachments 引用。', {'session': S, 'name': S, 'content_base64': S}, ['session', 'name', 'content_base64']),
    ('relay_team_download', '下载团队附件：≤5MB 返回 base64，更大的返回签名链接（15分钟有效，无需请求头）。', {'session': S, 'file_id': S}, ['session', 'file_id']),
]


def call(name, args, actor, teams, identity, client_ip=None):
    validate_actor(actor, identity)
    body = {key: value for key, value in args.items() if key != 'session'}
    if name == 'relay_team_create':
        return teams.create(actor, body)
    if name == 'relay_team_join':
        return teams.join(actor, body)
    if name == 'relay_team_gate_join':
        return teams.join_gate(actor, body, client_ip=client_ip)
    if name == 'relay_team_list':
        return teams.list_sessions(actor)
    sid = args.get('session')
    if name == 'relay_team_get':
        return teams.get(actor, sid)
    if name == 'relay_team_read':
        result = teams.events(actor, sid, args.get('after', 0), args.get('wait', 0))
        validate_actor(actor, identity)
        teams.get(actor, sid)
        return result
    if name == 'relay_team_invite':
        return teams.invite(actor, sid, body)
    if name == 'relay_team_invites':
        return teams.invites(actor, sid)
    if name == 'relay_team_invite_revoke':
        return teams.revoke_invite(actor, sid, args.get('invite_id'))
    if name == 'relay_team_invite_reset':
        return teams.reset_invites(actor, sid)
    if name == 'relay_team_gate_open':
        return teams.open_gate(actor, sid, body)
    if name == 'relay_team_gate_get':
        return teams.gate_status(actor, sid, args.get('role'))
    if name == 'relay_team_gate_close':
        return teams.close_gate(actor, sid, body)
    if name == 'relay_team_gate_rotate':
        return teams.rotate_gate(actor, sid, body)
    if name == 'relay_team_upload':
        raw = args.get('content_base64')
        if not isinstance(raw, str) or len(raw) > 14 * 1024 * 1024:
            raise ValueError('MCP附件最大10MB，更大文件请用 REST 上传')
        content = base64.b64decode(raw, validate=True)
        if len(content) > 10 * 1024 * 1024:
            raise ValueError('MCP附件最大10MB，更大文件请用 REST 上传')
        return teams.upload(actor, sid, args.get('name'), content)
    if name == 'relay_team_download':
        item, path = teams.locate_file(actor, sid, args.get('file_id'))
        payload = {"file_id": item["id"], "name": item["name"], "size": item["size"],
                   "sha256": item["sha256"], "type": item["type"]}
        if item["size"] > 5 * 1024 * 1024:
            payload.update(too_large=True, download_url=item["url"])
            return payload
        payload["content_base64"] = base64.b64encode(path.read_bytes()).decode()
        return payload
    method = {'relay_team_send': teams.send, 'relay_team_ack': teams.ack,
              'relay_team_heartbeat': teams.heartbeat,
              'relay_team_state': teams.state,
              'relay_team_revoke': teams.revoke}[name]
    return method(actor, sid, body)
