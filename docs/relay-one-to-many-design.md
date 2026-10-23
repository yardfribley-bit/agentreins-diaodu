# Relay 一对多设计说明（草案，未实施）

状态：设计评审稿，本轮不改代码。目标是把「一个 Topic 里一个 mentor + 一个 worker」扩展为「一个 mentor + 多个 worker」，让指挥端可以并行派活、分别收结果、统一汇总。

## 1. 席位模型

现状：`members` 表主键是 `(session_id, role)`，role 只有 owner/mentor/worker，因此每个会话每个角色最多一行；`UNIQUE(session_id, actor_key)` 保证一个身份只占一席。

目标：mentor 仍为一席（指挥端唯一），owner 仍不占席位；worker 变为 N 席（建议默认上限 8，可配置）。

数据结构（推荐方案 A）：

```sql
CREATE TABLE members (
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,            -- owner | mentor | worker
  seat INTEGER NOT NULL DEFAULT 0, -- mentor=0，worker=1..N
  actor_key TEXT NOT NULL,
  name TEXT NOT NULL,
  last_seen REAL NOT NULL,
  ack_seq INTEGER NOT NULL DEFAULT 0,
  environment TEXT,
  PRIMARY KEY(session_id, role, seat),
  UNIQUE(session_id, actor_key)
);
```

- 对外暴露 `seat`（如 `worker#2`），不暴露 `actor_key`。
- 加入 worker 时由服务端分配最小空闲 seat；mentor 和 owner 的 seat 固定 0。
- `topics/<码>` 的 `seats` 增加 `workers: [{seat, occupied, name}]`，保留 `seats.worker` 作为第一个 worker 的兼容别名。
- `session.peers` 保留现有 mentor/worker 项，另加 `workers` 数组（每个含 seat/name/online/ack_seq/environment）。

取舍：

- 方案 A（改 members 表）：改动集中，查询直观；需要一次 SQLite 表重建迁移。
- 方案 B（新建 seats 表 + members 存 actor）：更规范，但 join/权限/事件都要跨表，改动面大一倍。当前单进程规模用不上，不推荐。
- 方案 C（role 写成 worker:1）：字段语义脏，索引和权限判断都容易出错，排除。

## 2. 指令如何指定目标

推荐在消息上增加可选字段 `to`：

- 省略 / `null`：广播，所有 worker 都应收到的指令。
- `"worker:2"`：只发给 2 号 worker。
- 权限：`instruction` 仍只允许 mentor 或 owner 发送；指定不存在的 seat 返回 400；指定已被撤销的 seat 返回 409。
- worker 回 `progress`/`result` 时 `to` 默认 `"mentor"`；`question`/`note` 默认广播。

事件里记录 `message.to`，并在 `sender` 里加 `seat`。这样即使不引入私有通道，也能判断「这条指令原本给谁」。

取舍：

- 只广播：实现最简单，但指挥端无法只让 3 号去做 A、5 号去做 B，退化成群聊。
- 单值 `to`（推荐）：覆盖绝大多数派活场景，权限判断简单。
- `to` 数组（多播）：表达能力更强，但 ack/归属/界面都复杂，第一阶段不做。

## 3. 结果归属：reply_to 够不够

一对一时 `reply_to` + `sender.role=worker` 已经够用。一对多时需要三个信息才能准确归属：

1. 指令 seq：`reply_to`（已有）。
2. 执行者席位：`sender.seat`（新增）。
3. 指令目标：`message.to`（新增，或是广播）。

因此**`reply_to` 本身够定位「回的是哪条」，但不够区分「谁回的」和「是否越权回了别人的指令」**。建议：

- 服务端校验：worker 回 `result` 且 `reply_to` 指向定向给其他 seat 的指令时，返回 403（除非该指令是广播）。
- 汇总视图：按 `reply_to` 分组，每组下列出各 seat 的 result；未回报的 seat 显示为待办。
- 不引入 assignment 实体。派活就是一条 instruction，`to` 是目标，`reply_to` 是结果归属，足够轻。

如果以后要「一个指令拆成多个子任务、每子任务独立状态」，再引入 `assignment_id`；当前不做。

## 4. 谁读谁的：可见性

三个候选：

- **全可见（推荐第一阶段）**：所有成员看到同一条事件流。实现零改动（现在就是这样），worker 能看到其他 worker 的指令与结果，便于互相避让和补位；缺点是无关内容多、结果互相参考可能污染独立性。
- **定向消息仅相关方可见**：`to` 为 `worker:2` 的事件只对 sender/目标/mentor/owner 可见；`events` 按成员过滤。需要给事件加可见性并改 replay/游标语义（游标仍用全局 seq，跳过不可见事件），测试和页面都要改，工作量明显增大。
- **混合**：instruction 广播可见，result 仅 mentor/owner 与本 seat 可见。介于两者之间，但「谁看到了什么」的规则要写进契约，容易在评审时扯皮。

建议第一阶段全可见，先把多席位、定向、汇总跑通；把私有通道留成 `visibility` 字段的二期能力。若用户明确要求 worker 之间隔离，再走定向可见方案。

## 5. 向后兼容与迁移

- 数据库：重建 `members` 表，旧数据 `seat=0`；worker 的旧行就是 0 号席位。迁移在 `Relay.__init__` 里按版本号执行一次，迁移前备份 `relay.sqlite3`。
- 事件：旧事件没有 `sender.seat`、`message.to`，读取时补 `seat=0`、`to=null`；不重写历史。
- API：`role='worker'` 语义不变；`join` 响应新增 `seat`；`topic_info` 新增 `workers` 数组，保留 `seats.worker`；`session.peers` 新增 `workers`，保留原有 mentor/worker 项。旧客户端仍能加入、读事件、发消息。
- 客户端：`relay_client.py` 的 `join` 打印 seat；`send` 增加 `--to`；`watch` 不变（全局事件流）。
- 迁移策略：先上线「多席位 + 全可见 + to 单值」，观察一个版本；再决定是否加可见性或 assignment。
- 回滚：恢复旧代码 + 迁移前的 `relay.sqlite3` 备份；新表结构旧代码无法读取，所以回滚必须同时恢复数据库，不能只回代码。

## 6. 工作量与风险

- 存储与迁移：中（members 重建、seat 分配、权限校验）。
- REST/MCP：中（join 多席位、to 字段、revoke 按 seat、topic_info 扩展）。
- 页面：中（worker 列表、按 seat 汇总结果、派活目标选择）。
- 测试：大（多 worker 并发加入、seat 分配竞争、定向/广播、越权 result、迁移兼容、旧客户端回归）。
- 主要风险：`members` 表迁移不可逆；席位名称与 seat 编号的稳定性；大量 worker 时事件流噪声与 ack 语义；页面从「两端」改成「一拖 N」的信息密度。
