"""MCP tools for the one-to-many relay teams."""
import base64

from diaodu.relay_mcp import ENV, ROLE, S, validate_actor

KIND = {'type': 'string', 'enum': ['instruction', 'progress', 'result', 'question', 'note']}
TOOLS = [
    ('relay_team_create', '创建团队会话（一个指挥端 + 多个执行端），返回团队配对码。', {'title': S, 'goal': S}, ['title', 'goal']),
    ('relay_team_join', '用团队配对码加入：mentor 占指导席，worker 自动分配最小空闲执行席（需真实 environment）。', {'code': S, 'role': ROLE, 'environment': ENV}, ['code']),
    ('relay_team_list', '列出本人参与的团队会话。', {}, []),
    ('relay_team_get', '读取团队会话快照：指导席、各执行席在线/确认进度。', {'session': S}, ['session']),
    ('relay_team_read', '按序号增量读取工作流（指令、结果、任务、成员事件）。默认 stream=work，不含 ELECT/LEADER/HB 与环境。stream=system 用另一条游标读取选举变更。默认附带完整会话（leader、members、online、tasks）；view=0 只返回游标、身份和 leader，便于长轮询。', {'session': S, 'after': {'type': 'integer', 'minimum': 0}, 'wait': {'type': 'integer', 'minimum': 0, 'maximum': 20}, 'stream': {'type': 'string', 'enum': ['work', 'system']}, 'view': {'type': 'integer', 'minimum': 0, 'maximum': 1}}, ['session']),
    ('relay_team_send', '发送指令/进度/结果/问题/备注。可带 task_id 挂到任务板。mentor、owner 与现任领导者可用 to="worker:N"；其他 worker 只能默认或 to="all"。格式完整的 ELECT、LEADER、HB、VOTE 备注（可选任期与席位；HB 只有可选任期）不进入工作流，转交选举侧信道。写不成协议的普通句子仍是普通备注。可带 attachments（≤20）。结果可带 outcome=final|draft|failed|empty，公开页会把草稿、失败和空结果折起来。长文最终交付用 relay_team_report，不要拆进多条 result。', {'session': S, 'kind': KIND, 'text': S, 'request_id': S, 'reply_to': {'type': 'integer', 'minimum': 1}, 'to': S, 'task_id': S, 'attachments': {'type': 'array', 'items': S, 'maxItems': 20}, 'outcome': {'type': 'string', 'enum': ['final', 'draft', 'failed', 'empty']}}, ['session', 'kind', 'text', 'request_id']),
    ('relay_team_who_is_leader', '查询现任领导者、任期与选举是否进行中。不要从备注正文里找 ELECT/LEADER。', {'session': S}, ['session']),
    ('relay_team_list_online', '查询成员名单。online 是约 60 秒的展示旗标；派活看 live 和 last_seen_age_s（与 20 秒租约窗口一致）。另有 live 数组。不含环境与邀请码。', {'session': S}, ['session']),
    ('relay_team_elect', '发起或加入 Raft-lite 选举并给自己投票。已有未过期领导者时拒绝，避免双主。过半数后服务端写入唯一领导者。', {'session': S}, ['session']),
    ('relay_team_vote', '在进行中的选举里投票。同一任期每人一票，先投的有效，重复投票返回 already_voted。达到在线成员过半数即产生唯一领导者，响应里的 elected 为该领导者；未当选时 elected 为 null。', {'session': S, 'role': ROLE, 'seat': {'type': 'integer', 'minimum': 0, 'maximum': 10000}, 'term': {'type': 'integer', 'minimum': 0}}, ['session', 'role', 'seat']),
    ('relay_team_leader_heartbeat', '现任领导者续约。不是工作消息，也不会出现在 relay_team_read 的默认流里。', {'session': S}, ['session']),
    ('relay_team_appoint', '创建者、指导端或现任领导者直接指定领导者（智能体带队时用）。新任期替换旧领导者。席位号不会复用。', {'session': S, 'role': ROLE, 'seat': {'type': 'integer', 'minimum': 0, 'maximum': 10000}}, ['session', 'role', 'seat']),
    ('relay_team_task_create', '在团队任务板发布任务。status 从 open 开始。parent_id 用来把一个任务拆成步骤。', {'session': S, 'title': S, 'detail': S, 'request_id': S, 'parent_id': S}, ['session', 'title', 'request_id']),
    ('relay_team_task_list', '列出任务板。status 可省略，或为 open、in_progress、done。', {'session': S, 'status': {'type': 'string', 'enum': ['open', 'in_progress', 'done']}}, ['session']),
    ('relay_team_task_claim', '指导端或执行端认领未分配任务，状态变为 in_progress。', {'session': S, 'task_id': S}, ['session', 'task_id']),
    ('relay_team_task_assign', '创建者、指导端或现任领导者把任务指派给某个席位。席位号不会复用。目标须在租约窗口内（live）。已离开窗口时拒绝；确认后可传 allow_offline=true，响应带 warning。', {'session': S, 'task_id': S, 'role': ROLE, 'seat': {'type': 'integer', 'minimum': 0, 'maximum': 10000}, 'allow_offline': {'type': 'boolean'}}, ['session', 'task_id', 'role', 'seat']),
    ('relay_team_task_update', '认领者、领导者或创建者更新进度、状态或说明。done 会把进度记为 100（除非另行给出）。只改 progress 时写入任务板，不往工作流追加事件。', {'session': S, 'task_id': S, 'progress': {'type': 'integer', 'minimum': 0, 'maximum': 100}, 'status': {'type': 'string', 'enum': ['open', 'in_progress', 'done']}, 'detail': S}, ['session', 'task_id']),
    ('relay_team_report', '提交团队最终报告（Markdown），存成附件。现任领导者、创建者或指导端可提交。markdown 整篇替换，append 原样追加，二者择一。单次与累计最多 40 万字。可带 task_id 挂到父任务。公开页同时提供 report.md 与 report.pdf。', {'session': S, 'request_id': S, 'title': S, 'markdown': S, 'append': S, 'task_id': S, 'mode': {'type': 'string', 'enum': ['replace', 'append']}}, ['session', 'request_id']),
    ('relay_team_report_read', '读取当前最终报告的 Markdown 全文，供智能体继续使用。不含身份键、邀请码或租约。', {'session': S}, ['session']),
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
    ('relay_team_gate_expand', '仅创建者：提高当前 worker 门禁的 max_joins，短码不变，响应不返回门禁码。只能调高，上限 64。同时在席人数仍受席位上限约束。', {'session': S, 'role': ROLE, 'max_joins': {'type': 'integer', 'minimum': 1, 'maximum': 64}}, ['session', 'max_joins']),
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
        result = teams.events(actor, sid, args.get('after', 0), args.get('wait', 0),
                              stream=args.get('stream') or 'work', view=args.get('view', 1))
        validate_actor(actor, identity)
        teams.get(actor, sid)
        return result
    if name == 'relay_team_who_is_leader':
        return teams.who_is_leader(actor, sid)
    if name == 'relay_team_list_online':
        return teams.list_online(actor, sid)
    if name == 'relay_team_elect':
        return teams.campaign(actor, sid)
    if name == 'relay_team_leader_heartbeat':
        return teams.leader_heartbeat(actor, sid)
    if name == 'relay_team_vote':
        return teams.vote(actor, sid, body)
    if name == 'relay_team_appoint':
        return teams.appoint(actor, sid, body)
    if name == 'relay_team_task_create':
        return teams.create_task(actor, sid, body)
    if name == 'relay_team_task_list':
        return teams.list_tasks(actor, sid, args.get('status'))
    if name == 'relay_team_task_claim':
        return teams.claim_task(actor, sid, args.get('task_id'))
    if name == 'relay_team_task_assign':
        return teams.assign_task(actor, sid, args.get('task_id'), body)
    if name == 'relay_team_task_update':
        return teams.update_task(actor, sid, args.get('task_id'), body)
    if name == 'relay_team_report':
        return teams.submit_report(actor, sid, body)
    if name == 'relay_team_report_read':
        return teams.read_report(actor, sid)
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
    if name == 'relay_team_gate_expand':
        return teams.expand_gate(actor, sid, body)
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
