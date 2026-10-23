# 团队协作（一对多）：席位邀请制

团队模式与一对一并存但完全隔离：独立 `relay_teams.sqlite3`、独立表（`team_sessions` / `team_members` / `team_events` / `team_files` / `team_invites` / `team_revoked`）、独立附件目录 `relay-team-files/` 与密钥 `relay-team-files.key`、独立路由 `/v1/relay/teams/...`。一对一 relay 的数据和表不受影响。

## 成员与席位

- 一个团队会话有一个 owner（创建者，不占席位）、一个 mentor（指导席 seat 0）、最多 8 个 worker（seat 1..8，`DIAODU_RELAY_MAX_WORKERS` 可调）。
- `create` 不再返回通用配对码。owner 通过邀请把链接发给具体的人，一席一码。

## 邀请

- `POST /v1/relay/teams/<sid>/invites` `{role, seat?, note?}`：owner 生成邀请。worker 指定 `seat` 时绑定该席；不指定表示任意空席（加入时分配最小空闲号）。mentor 邀请固定 seat 0。默认 24 小时过期，`note` 用来标注用途（如“Alice 的笔记本”）。
- 返回 `{invite:{id, role, seat, note, status, created_at, expires_at, code}}`；`code` 只在创建时返回一次。
- 校验顺序：码不存在 → 过期 → 已撤销 → 已使用 → 席位已空。错误分开：无效/过期/撤销 403，已使用/席位被占/席位已满 409。
- 使用后写入 `used_by`（展示名 + 时间），码立即失效；同一码第二次使用返回 409。
- `GET /v1/relay/teams/<sid>/invites`：owner 的邀请账本，含身份、备注、状态（unused/used/revoked/expired）、使用人、生成与过期时间；不返回 code。
- `POST .../invites/<invite_id>/revoke`：撤销单条未使用邀请（已使用返回 409）。
- `POST .../invites/reset`：紧急止损，撤销全部未使用邀请。
- 撤销某席位时，该席位所有未使用邀请一并作废，并落席位墓碑；被撤销身份即使拿到新邀请也不能重入（403）。

## 加入与可见性

`POST /v1/relay/teams/join` `{code, role?, environment?}`：worker 必须提交真实系统环境；角色以邀请码为准，显式 role 不一致返回 400。加入响应带 `seat`。

可见性（服务端过滤，游标仍用全局 seq，允许空洞）：

- 广播 instruction（`to` 省略或 `"all"`）与系统事件（joined/environment/state/revoked）全体可见。
- 定向 instruction（`to="worker:N"`）只有 mentor、owner、目标席、发送者可见；worker 不能发起定向，`to="worker:N"` 返回 403。
- worker 的 result/progress/question/note 默认只有 mentor、owner、本人可见；显式 `to="all"` 才广播。
- 定向给已撤销席位：409；给不存在席位：400。

## 汇总

worker 的 result 用 `reply_to` 指回指令；mentor 按 `reply_to` 分组即可看到各席结果与未回报的席位。`sender.seat` 标明结果来自哪一席。

## 附件

`POST /v1/relay/teams/<sid>/files?name=` 上传（≤100MB，成员凭证）；`GET .../files/<file_id>` 需要 Bearer 或签名链接（与一对一同一套 HMAC 实现，15 分钟、密钥持久化）。事件里的 `message.attachments` 返回绝对签名 `url`，老事件给空数组。MCP 提供 `diaodu_relay_team_upload`（≤10MB）与 `diaodu_relay_team_download`（≤5MB 返回 base64，更大返回签名链接）。

## MCP 工具

`diaodu_relay_team_create` / `join` / `list` / `get` / `read` / `send` / `ack` / `heartbeat` / `state` / `revoke` / `invite` / `invites` / `invite_revoke` / `invite_reset` / `upload` / `download`。MCP 只需配置地址，`diaodu_connect` 后同一会话自动复用身份，无需 Bearer。

## 演示

```sh
/usr/local/bin/python3.11 scripts/team_demo.py
```

演示一个 mentor + 三个 worker：广播一条、定向一条给 2 号席，验证 1/3 号读不到定向指令、2 号能读并回结果，指挥端按 reply_to 汇总并列出未回报席位。9/9 检查通过。
