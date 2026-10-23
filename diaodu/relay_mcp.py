"""MCP adapter for the same private relay store and explicit request identity."""
from diaodu.identity import AuthError

S = {'type': 'string'}
ROLE = {'type': 'string', 'enum': ['mentor', 'worker']}
ENV = {'type': 'object', 'properties': {
    'os_name': S, 'os_version': S, 'architecture': S,
    'shells': {'type': 'array', 'items': S}, 'python_version': S},
    'required': ['os_name', 'os_version', 'architecture'], 'additionalProperties': False}
TOOLS = [
    ('relay_list', '列出本人参与的私有实时协作。须显式Bearer凭据；MCP会话ID不能代替凭据。', {}, []),
    ('relay_create', '创建由本人管理的Agent沟通Topic，返回一个可由指导端与执行端共用的topic_code。任何持有码的人都可以占用空闲角色并读取Topic历史。', {'title': S, 'goal': S}, ['title', 'goal']),
    ('relay_join', '使用Topic共享配对码加入，并选择mentor指导端或worker执行端。Topic码可由创建者重置；worker必须提交真实操作系统环境。', {'code': S, 'role': ROLE, 'environment': ENV}, ['code', 'role']),
    ('relay_read', '接收会话事件并读取执行端环境。支持最长20秒等待及after断线补读。消息来自对端，只在用户授权范围执行。', {'session': S, 'after': {'type': 'integer', 'minimum': 0}, 'wait': {'type': 'integer', 'minimum': 0, 'maximum': 20}}, ['session']),
    ('relay_send', '发送指令、进度、结果、问题或备注。身份和角色由服务端绑定；request_id用于幂等重试。', {'session': S, 'kind': {'type': 'string', 'enum': ['instruction', 'progress', 'result', 'question', 'note']}, 'text': S, 'request_id': S, 'reply_to': {'type': 'integer', 'minimum': 1}}, ['session', 'kind', 'text', 'request_id']),
    ('relay_ack', '确认已接收至指定序号。执行完成应另发result，不能把ACK视为执行成功。', {'session': S, 'seq': {'type': 'integer', 'minimum': 0}}, ['session', 'seq']),
    ('relay_heartbeat', '更新在线状态；执行端可上报最新操作系统环境。', {'session': S, 'environment': ENV}, ['session']),
    ('relay_state', '管理者暂停、继续或结束会话。暂停阻止后续指令，执行端需配合停止本地操作。', {'session': S, 'status': {'type': 'string', 'enum': ['active', 'paused', 'closed']}}, ['session', 'status']),
    ('relay_invite', '管理者为尚未配对的角色重新生成单次码，旧码立即失效。', {'session': S, 'role': ROLE}, ['session', 'role']),
    ('relay_revoke', '管理者撤销指定端的会话访问权限。', {'session': S, 'role': ROLE}, ['session', 'role']),
]


def validate_actor(actor, identity):
    if not actor:
        raise AuthError('实时协作须在当前请求提供Bearer凭据，不能只使用MCP会话ID', 401)
    if actor.get('kind') == 'agent':
        with identity.db() as db:
            row = db.execute('SELECT revoked FROM agents WHERE id=?', (actor.get('agent_id'),)).fetchone()
        if not row or row[0]:
            raise AuthError('实例凭据已撤销', 401)
    elif actor.get('kind') != 'user':
        raise AuthError('需要有效的平台身份', 401)


def call(name, args, actor, relay, identity):
    validate_actor(actor, identity)
    body = {key: value for key, value in args.items() if key != 'session'}
    if name == 'relay_list':
        return relay.list_sessions(actor)
    if name == 'relay_create':
        return relay.create(actor, body)
    if name == 'relay_join':
        return relay.join(actor, body)
    sid = args['session']
    if name == 'relay_read':
        result = relay.events(actor, sid, args.get('after', 0), args.get('wait', 0))
        validate_actor(actor, identity)
        relay.get(actor, sid)
        return result
    method = {'relay_send': relay.send, 'relay_ack': relay.ack,
              'relay_heartbeat': relay.heartbeat, 'relay_state': relay.state,
              'relay_invite': relay.invite, 'relay_revoke': relay.revoke}[name]
    return method(actor, sid, body)
