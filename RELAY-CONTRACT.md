# 实时协作接口（第一版实现约定）

独立页面 `/task/relay/`，API 前缀 `/task/v1/relay`；本机服务去掉 `/task`。
复用平台现有 Bearer / HttpOnly cookie + CSRF 身份。所有会话和消息仅 owner / 已配对成员可读。
Topic 由管理者创建并生成一个共享配对码。加入者提供 Topic 码并选择 mentor（指导端）或 worker（执行端）；每个角色一席。配对码可重复使用，直至 Topic 结束或管理者重置。管理者可手动发送指令、暂停/继续/结束、撤销成员。服务器不运行模型和系统命令。

## REST

- GET `/v1/relay/sessions` → `{sessions:[session]}`
- POST `/v1/relay/sessions` `{title,goal}` → `{session,topic_code}` (201)
- POST `/v1/relay/join` `{code,role,environment?}` → `{session}`。Topic码需提供role；worker必须提供environment，mentor不发送。
- GET `/v1/relay/sessions/:id` → `{session}`
- GET `/v1/relay/sessions/:id/events?after=0&wait=20` → `{session,events:[event],next_cursor,has_more}`。wait 0..20 秒，实时唤醒长轮询，每页最多100项。
- POST `/v1/relay/sessions/:id/messages` `{kind,text,request_id,reply_to?,attachments?}` → `{event,duplicate}`。request_id 必填，幂等重试；同ID不同内容409。`attachments` 为本会话附件编号数组，最多20个。
- POST `/v1/relay/sessions/:id/files?name=<URL编码>` 原始字节 → `{file_id,name,size,sha256}` (201)。成员凭证；单文件≤100MB。文件名禁止路径分隔符/控制字符，长度≤240。
- GET `/v1/relay/sessions/:id/files/:file_id` 下载。成员凭证；私有，非成员403；跨会话编号404；`X-Content-Type-Options: nosniff`，类型按扩展名与文件头双判，仅图片/视频/纯文本可内联，其余作为附件下载。
- POST `/v1/relay/sessions/:id/ack` `{seq}` → `{acknowledged:seq}`。
- POST `/v1/relay/sessions/:id/heartbeat` `{environment?}` → `{session}`。只有worker可上报/更新environment。
- POST `/v1/relay/sessions/:id/state` `{status:'active'|'paused'|'closed'}` → `{session}`。仅owner；closed终态。
- POST `/v1/relay/sessions/:id/invites` `{role:'mentor'|'worker'}` → `{role,code,expires_at}`。仅owner且角色空闲。
- POST `/v1/relay/sessions/:id/topic-code` `{}` → `{code}`。仅owner；立即重置共享 Topic 码，旧码失效。
- POST `/v1/relay/sessions/:id/revoke` `{role}` → `{session}`。仅owner；撤销即失去读写权限。被撤销的实例不能用原 Topic 码重新加入（含换角色），管理员如需重新接纳，须重新签发一次性邀请码。

session: `{id,title,goal,status,created_at,updated_at,owner:boolean,role:'owner'|'mentor'|'worker',peers:[{role,name,online,last_seen,ack_seq}],last_seq}`。

event: `{seq,kind:'message'|'joined'|'state'|'revoked'|'environment',created_at,sender:{role,name},message?:{kind,text,reply_to,attachments},status?,role?,environment?}`。`message.attachments` 每项为 `{id,name,size,sha256,type,uploader,created_at,url}`；没有附件的历史事件返回 `[]`，不会缺字段。

附件与事件解耦：先上传拿 `file_id`，发送消息时引用；文件落在 data/library/relay-files/，元数据在 relay.sqlite3 的 files 表；字节在锁外流式写入并计算 sha256，锁内只登记元数据。下载必须持有会话成员凭证，不能像公开任务附件那样匿名读取。

执行端必须在join时上报environment：`{os_name,os_version,architecture,shells?:[],python_version?}`，前三项必填。字段只描述系统环境，不上传机器名、用户名、密钥或完整环境变量。worker的`peers[].environment`显示最新信息；heartbeat更新时仅有变化才追加environment事件。页面注明为执行端自报。

消息kind: `instruction`（mentor或owner）、`progress`/`result`（worker）、`question`/`note`（全部成员）。身份/角色取服务端绑定，拒绝客户端伪造字段。paused禁止新instruction，仍允许结果/进度回报；closed禁止新消息和加入，但历史可读。暂停不宣称能强停远端已执行的命令。

## Python store 接口

`Relay(root: Path)` 使用 `root / 'relay.sqlite3'`；私有数据库0600，独立表，不改board数据。

方法均返回上述JSON可序列化对象，错误用AuthError(message,status)或ValueError：

`list_sessions(actor)`, `create(actor,body)`, `join(actor,body)`, `get(actor,sid)`,
`events(actor,sid,after=0,wait=0)`, `send(actor,sid,body)`, `ack(actor,sid,body)`,
`heartbeat(actor,sid,body=None)`, `state(actor,sid,body)`, `invite(actor,sid,body)`, `revoke(actor,sid,body)`。

同一make_handler创建一次store挂到library.relay。actor键使用`agent_id`（agent）或`user_id`（user），不能以user_id合并不同agent。成员名只存公开展示名，不存token。owner身份不自动占用两Agent角色。

Topic 配对码至少192位随机，仅哈希存储，可重复用于尚未占用的角色；只有管理者可重置，结束会话后不可加入。旧版签发的单次角色码仍按30分钟有效。单一身份不能同时占用mentor与worker。使用SQLite事务实现会话内顺序号与幂等性；条件变量唤醒长轮询并限制并发等待数。每轮wait及返回前重新核对成员资格。单消息<=8000字符、单会话<=5000事件、每管理者最多20个未关闭会话；输出消息必须使用textContent避免XSS。

`peers[].online` 以最近60秒内是否有任何已认证请求为准，长轮询本身也算，因此只旁听的一端仍在线；`ack_seq` 只表示已接收，不代表已执行。消息文本入库前去掉首尾空白，幂等哈希按规范化文本计算，空白差异不产生新的409。普通事件达到上限后拒绝新消息；结束、撤销等控制事件不受上限影响，执行端环境刷新只更新成员信息、不再追加事件且不返回409。

## 接入

REST适用于任意可HTTP调用的Agent；提供标准库Python客户端和MCP工具（现有 `/task/v1/mcp`）。接入端负责接收指令、在自身权限范围执行、回传结果，平台不自动托管模型。文档说明ACK表示已接收，执行完成须另发result；断线按cursor补读，执行端应持久化已执行指令ID以免重试重复执行。
