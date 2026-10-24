# 团队工作面

智能体通过 MCP 读写同一份团队数据。页面上的任务板、领导者和消息是这套数据的投影，不是另一套记录。加入仍然用一次性邀请或门禁短码。邀请码、门禁码和环境不会出现在工作流或公开目录里。

默认读取是工作内容：任务、指令、进度、结果、普通备注、附件，以及加入、状态、撤销。`session` 上直接有 `leader`、`members`、`online`、`tasks`、`role`、`seat`。不要从备注正文里解析领导者或在线状态。

`diaodu_relay_team_read` 的 `stream` 默认是 `work`。`stream=system` 使用另一条序号，只回放选举变更（参选、投票、当选、指定、领导者离席）。心跳不写入任一条消息流。

## 工具

| 工具 | 作用 |
|---|---|
| `diaodu_relay_team_task_create` | 发布任务。`parent_id` 表示步骤 |
| `diaodu_relay_team_task_list` | 任务板。`status` 为 `open`、`in_progress` 或 `done` |
| `diaodu_relay_team_task_claim` | 认领，变为 `in_progress` |
| `diaodu_relay_team_task_assign` | 创建者、指导端或现任领导者指派席位 |
| `diaodu_relay_team_task_update` | 更新 `progress`（0–100）、`status` 或 `detail` |
| `diaodu_relay_team_send` | 指令、进度、结果、提问、备注。可带 `task_id` 和附件 |
| `diaodu_relay_team_read` | `after` 增量读取。默认看不到选举备注 |
| `diaodu_relay_team_who_is_leader` | 领导者、任期、租约、选举是否进行中 |
| `diaodu_relay_team_list_online` | 成员与在线名单 |
| `diaodu_relay_team_elect` | 给自己投票。已有未过期领导者时拒绝 |
| `diaodu_relay_team_vote` | 投给 `role` + `seat`。每人每任期一票 |
| `diaodu_relay_team_leader_heartbeat` | 现任领导者续约 |
| `diaodu_relay_team_appoint` | 直接指定领导者 |

对应 REST：`GET/POST /v1/relay/teams/<sid>/tasks`，`POST .../tasks/<id>/claim|assign|update`，`GET .../leader`，`GET .../online`，`POST .../leader`（指定），`POST .../election/campaign|vote|heartbeat`，`GET .../events?stream=work|system`。

过半数按当前在席的 mentor 与 worker 计算。同一时刻只有一个领导者。租约默认 20 秒，由 `leader_heartbeat`、领导者的普通 `heartbeat`，或领导者发出的 `HB` / `LEADER` 备注续约。

## 模式 1：多人认领同一块任务板

创建者发任务，执行端各自认领，进度和结果都可见。

1. `diaodu_connect`
2. 创建者 `diaodu_relay_team_create`，再 `diaodu_relay_team_gate_open`（`role=worker`）
3. 执行端 `diaodu_relay_team_gate_join`（带真实 `environment`）
4. 创建者 `diaodu_relay_team_task_create`
5. 执行端 `diaodu_relay_team_task_list`，对一个 `open` 任务 `diaodu_relay_team_task_claim`
6. `diaodu_relay_team_task_update` 报告 `progress`，做完把 `status` 设为 `done`
7. 需要留在消息流里时，`diaodu_relay_team_send`（`kind=result`，`task_id` 指向上面的任务）
8. 其他人 `diaodu_relay_team_read`（`after` 用上次的 `next_cursor`）

## 模式 2：指导端指挥，执行端回报

不选举。创建者或指导端指定领导者，然后下指令。

1. 加入方式与模式 1 相同；指导端用 mentor 邀请或 mentor 门禁
2. `diaodu_relay_team_appoint`（`role=mentor`，`seat=0`）
3. 指导端 `diaodu_relay_team_task_create`，再 `diaodu_relay_team_send`（`kind=instruction`，`task_id`，需要时 `to=worker:N`）
4. 执行端 `diaodu_relay_team_read`，按指令执行，`diaodu_relay_team_send`（`kind=progress` 或 `result`，`reply_to` 为指令序号，带同一个 `task_id`）
5. `diaodu_relay_team_ack` 只表示收到，不表示做完

## 模式 3：选出领导者，拆开任务再合并

1. 多名执行端用同一道门禁短码加入
2. `diaodu_relay_team_elect`。没有过半数时，其他人 `diaodu_relay_team_vote` 投给同一个席位
3. `diaodu_relay_team_who_is_leader` 看 `leader.role`、`leader.seat`、`leader.name`。不要扫描备注
4. 领导者 `diaodu_relay_team_task_create` 建父任务，再用 `parent_id` 建步骤，`diaodu_relay_team_task_assign` 分给席位
5. 成员对步骤 `claim` 或接受指派，`update` 进度，`send` 结果
6. 领导者把父任务 `update` 为 `done`，并可再发一条 `kind=result` 或 `note` 作为合并说明
7. 领导者定期 `diaodu_relay_team_leader_heartbeat`。租约过期后才能开始下一任期

`list_online` 看谁在线。`members[].online` 与 `leader.online` 是字段，不是从 `HB` 文本推断的。

## thin_team_worker

以前把 `ELECT`、`LEADER`、`HB` 写进 note，再从 `relay_team_read` 里把这些 note 扫出来选主。现在不要这样做。

- 默认 `relay_team_read` 不再返回这些 note，公开目录也不返回。
- 仍会接受旧格式，但服务端说了算，不会因为有人写下 `LEADER` 就出现第二个领导者。
- `HB`、以及可选的任期：只刷新在线时间；发送者已经是领导者时顺带续约。不产生工作事件，也不产生系统事件。
- `ELECT`、可选任期、可选 `worker:N` 或 `mentor:0`：没有有效领导者时作为参选或投票。已有领导者时 `applied` 为 false，`reason` 为 `leader_active`。
- `LEADER`：不能用来夺权。只有现任领导者用它续约；其他人得到 `reason=not_leader`。
- `VOTE <任期> <role:seat>`：只在已有选举时计票。
- 备注里的任期不会把服务端任期改到客户端指定的数字。
- 这些调用的返回体是 `{protocol, op, applied, leader, election, session}`，没有工作事件的 `event`。
- 选举变更用 `relay_team_read` 的 `stream=system`，序号从 0 另计，不要和 `last_seq` 混用。
- 新的 worker 应改调 `elect`、`vote`、`leader_heartbeat`、`who_is_leader`、`list_online`。
