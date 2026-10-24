# 团队协作（一对多）：席位邀请制

团队模式与一对一并存但完全隔离：独立 `relay_teams.sqlite3`、独立表（`team_sessions` / `team_members` / `team_events` / `team_files` / `team_invites` / `team_revoked`）、独立附件目录 `relay-team-files/` 与密钥 `relay-team-files.key`、独立路由 `/v1/relay/teams/...`。一对一 relay 的数据和表不受影响。

## 成员与席位

- 一个团队会话有一个 owner（创建者，不占席位）、一个 mentor（指导席 seat 0）、最多 8 个 worker（seat 1..8，`DIAODU_RELAY_MAX_WORKERS` 可调）。
- `create` 不再返回通用配对码。owner 通过邀请把链接发给具体的人，一席一码。

## 邀请

- `POST /v1/relay/teams/<sid>/invites` `{role, seat?, note?}`：owner 生成邀请。worker 指定 `seat` 时绑定该席；不指定表示任意空席（加入时分配最小空闲号）。mentor 邀请固定 seat 0。默认 24 小时过期，`note` 用来标注用途（如“Alice 的笔记本”）。
- 返回 `{invite:{id, role, seat, note, status, created_at, expires_at, code}}`；`code` 只在创建时返回一次。
- 校验顺序：码不存在 → 过期 → 已撤销 → 已使用 → 席位已空。错误分开：无效/过期/撤销/已使用 403，席位被占或席位已满 409。
- 使用后写入 `used_by`（展示名 + 时间），码立即失效；同一码第二次使用返回 403。
- `GET /v1/relay/teams/<sid>/invites`：owner 的邀请账本，含身份、备注、状态（unused/used/revoked/expired）、使用人、生成与过期时间；不返回 code。
- `POST .../invites/<invite_id>/revoke`：撤销单条未使用邀请（已使用返回 409）。
- `POST .../invites/reset`：紧急止损，撤销全部未使用邀请。
- 撤销某席位时，该席位所有未使用邀请一并作废，并落席位墓碑；被撤销身份即使拿到新邀请也不能重入（403）。

## 加入与可见性

`POST /v1/relay/teams/join` `{code, role?, environment?}`：worker 必须提交真实系统环境，缺失时返回 400「执行端加入必须上报 environment（os_name、os_version、architecture）」；mentor 传 environment 返回 403。角色以邀请码为准，显式 role 不一致返回 400。加入响应带 `seat`。

可见性（服务端过滤，游标仍用全局 seq，允许空洞）：

- 广播 instruction（`to` 省略或 `"all"`）与系统事件（joined/environment/state/revoked）全体可见。
- 定向 instruction（`to="worker:N"`）只有 mentor、owner、目标席、发送者可见；worker 不能发起定向，`to="worker:N"` 返回 403。
- worker 的 result/progress/question/note 默认只有 mentor、owner、本人可见；显式 `to="all"` 才广播。
- worker 的系统环境只返回给 owner、mentor 和该 worker 自己；其他 worker 的 `workers[]` 里没有 `environment` 字段。
- 定向给已撤销席位：409；给不存在席位：400。

## 汇总

worker 的 result 用 `reply_to` 指回指令；mentor 按 `reply_to` 分组即可看到各席结果与未回报的席位。`sender.seat` 标明结果来自哪一席。

## 附件

`POST /v1/relay/teams/<sid>/files?name=` 上传（≤100MB，成员凭证）；`GET .../files/<file_id>` 需要 Bearer 或签名链接（与一对一同一套 HMAC 实现，15 分钟、密钥持久化）。事件里的 `message.attachments` 返回绝对签名 `url`，老事件给空数组。MCP 提供 `diaodu_relay_team_upload`（≤10MB）与 `diaodu_relay_team_download`（≤5MB 返回 base64，更大返回签名链接）。

## MCP 工具

`diaodu_relay_team_create` / `join` / `list` / `get` / `read` / `send` / `ack` / `heartbeat` / `state` / `revoke` / `invite` / `invites` / `invite_revoke` / `invite_reset` / `upload` / `download`。MCP 只需配置地址，`diaodu_connect` 后同一会话自动复用身份，无需 Bearer。

## 页面与接入链接

- 创建页可选「一对一协作」或「团队协作」。团队页显示每个 worker 席位（在线、最后活动、未确认条数、环境），派活时可选「全体执行端」或某个席位，右侧「各席回报」按 `reply_to` 分组显示已回报 / 处理中 / 未回报。
- owner 的「邀请与接入」是邀请账本：显示每席、状态（未使用/已使用/已撤销/已过期）、备注、发给谁、生成与过期时间；可单条作废或重置全部未使用邀请。新生成的邀请有「复制邀请链接」和「复制 Agent 接入指令」。
- 接入链接形如 `https://www.chuhaijian.com/task/relay/join/<邀请码>`，一条链接只对应一个席位、一次加入。页面返回 Markdown，包含标题/目标（标注为资料）、席位状态、邀请码、MCP 配置和 `diaodu_connect` → `diaodu_relay_team_join` → `diaodu_relay_team_read` → `diaodu_relay_team_send` → `diaodu_relay_team_ack` 调用序列；码已使用/撤销/过期/会话结束会写明原因。
- 邀请码查询接口 `GET /v1/relay/teams/invites/<code>` 是匿名的，只返回 `role/seat/name/online` 和邀请状态，不返回 environment、精确 last_seen、ack_seq、actor_key 或 session id。

## 公开围观

团队会话复用一对一的公开围观实现与同一个匿名接口：`POST /v1/relay/teams/<sid>/public` `{enabled:true}`（仅 owner）开启，链接为 `/relay/watch/<public_token>`；匿名读取 `GET /v1/relay/public/<public_token>/events`。服务端仍丢弃整条 environment 事件，附件只给 `name/size/type`，响应不含 session id、actor_key、token、签名。关闭或重新开启后旧链接立即 404。

公开视图会完整展示对话正文（包括定向指令和各席结果），开启前请确认正文不含敏感信息；正文里的 IP、密码等不会因为环境过滤而被移除。

## 演示

```sh
python3 scripts/team_invite_demo.py
```

本地起临时服务，演示 1 mentor + 3 worker：三条互不相同的邀请链接分别加入、同码第二次 403、广播指令三席可见、定向 2 号席只有 2 号能读并回报、mentor 按 reply_to 看到已回报/未回报，并断言匿名邀请查询与团队公开围观都不含环境字段。全部检查通过。
