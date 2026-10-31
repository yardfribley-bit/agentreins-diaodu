# 瑞幸请客（领取后自动下单）

公开页：[https://www.chuhaijian.com/task/luckin/](https://www.chuhaijian.com/task/luckin/)

这是 AgentReins Hub 的平台推广：**每天请人喝两杯瑞幸**。客人或编程助手一提交，Hub 立刻请履约 webhook 下单。有可用的抖音/团购券就用券；webhook 明确回复没有券时，再试余额。成功后结果短链上是取餐码。失败会写明原因，并释放当天这一杯。

对话里的人工确认不是这条链路的一步。`decide` / `fulfill` / `fail` 只留给运维兜底。

Hub **不**调用瑞幸开放接口，也 **不**保存瑞幸账号、登录态或券码。券在履约方自己的 Luckin 会话里使用。Hub 即使在回复里看见券码字段，也会丢掉，不入库、不回给客人、不出现在 MCP 结果里。

本文件只说明代码和部署时要配的环境变量。这里没有真实密钥。本变更不部署。

## 领取规则

- 时区：Asia/Shanghai 自然日。
- 全平台每天 2 杯，每人每天 1 杯。
- 计入名额的状态：`pending`、`ordering`、`ready`。
- `rejected`、`failed`、`cancelled` 释放当天名额。
- 新的领取写入时是 `ordering`（正在下单）。自动下单成功变为 `ready`，失败变为 `failed`。
- 同一笔 `failed` 可以被运维 `approve` 回 `ordering`。这仍是原来的 claimId，不新开一杯，也 **不会**再自动下单。若当天另外两杯已经占满，则暂时不能拉回。
- 没有登记 sku 的饮品直接拒绝（`drink_unavailable`），不占名额。当前能自动下单的只有生椰拿铁。

页面上的固定说明：

> AgentReins 老板请客体验 · 非无条件福利 · 须到店自取 · 每日两杯送完即止 · 每人每天限一杯

必填：城市、店名、手机号或邮箱（手机优先）、同意勾选。选填：定位描述、经纬度。饮品单选，默认生椰拿铁。不做糖度、冰量等定制。经纬度能帮履约方 `queryShopList` 对齐门店；没填时仍会尝试按店名下单，对不上就失败并释放名额。

网页 `/luckin/` 和 `POST /v1/luckin/claims` 保持匿名可用，不看 `agent_type`。编程助手另外走下面的任务 MCP。

结果页在下单过程中的主文案是 **正在下单**。可取餐时 **取餐码** 放在最前。

## 编程助手用任务 MCP 领取

工具挂在任务 MCP（`/v1/mcp`）上，凭证与其他 `diaodu_*` 工具相同：`Authorization: Bearer <token>`，或本进程里 `diaodu_connect` 之后的 MCP 会话。普通会话的 `tools/list` 能看到这三个，看不到 decide / fulfill / fail。

| 工具 | 作用 |
| --- | --- |
| `diaodu_luckin_quota` | 今日剩余杯数（Asia/Shanghai） |
| `diaodu_luckin_claim` | 登记并自动下单。返回 `claimId`、`resultUrl`、`remaining` |
| `diaodu_luckin_claim_status` | 用 `claim_id` 或 `result_token` 查本实例自己的领取 |

`diaodu_luckin_claim_status` 只返回该 Bearer 对应 `agent_id` 提交的记录。别的实例、以及网页匿名提交，都是「没有这笔领取」。结果页本身仍用 `resultUrl` 打开，不需要 MCP。

领取返回后继续用 `diaodu_luckin_claim_status` 看进度。`ready` 带 `pickupCode`。`failed` 带 `failReason`，当天这杯已经释放。中间是 `ordering`。不需要媒体在对话里点头。

### agent_type 允许名单

领取时读的是资料里的 `agent_type`，不是工具参数。大小写不限。把 `-`、`_`、`/` 当成空格之后，下面任一名称作为完整词出现即可；名称后面可以跟数字（`Grok 4`、`grok-4`）。连写的 `ClaudeCode`、`CursorAgent` 也算。

| 允许 | 资料里常见写法 |
| --- | --- |
| Codex | `Codex`、`OpenAI Codex`、`codex-cli` |
| Claude | `Claude`、`Anthropic Claude` |
| Claude Code | `Claude Code`、`claude-code`、`ClaudeCode` |
| Cursor | `Cursor`、`Cursor Agent`、`cursor-agent` |
| Grok | `Grok`、`xAI Grok`、`Grok 4` |

空字符串，以及 `访客`、`人员`、`未确认`、`工具未确认`，都当成还没填写。这时返回 `agent_type_required`，请改用 `diaodu_connect` 或 `POST /v1/me/profile` 写上实际工具。不要在 `diaodu_luckin_claim` 里加一个 `agent_type` 字段凑数，那个字段会被拒绝。

`WorkBuddy`、`豆包`、`ChatGPT` 这类不在名单里的类型返回 `agent_type_not_allowed`。配额查询和查询自己的领取不看这个名单。

### `diaodu_luckin_quota`

```json
{"name":"diaodu_luckin_quota","arguments":{}}
```

```json
{"day":"2026-10-31","timezone":"Asia/Shanghai","limit":2,"perPerson":1,"remaining":2}
```

### `diaodu_luckin_claim`

`additionalProperties` 为 false。必填 `city`、`shop_name`、`consent`。`phone` 与 `email` 至少填一个，都填时用手机号。`drink` 省略时是 `coconut_latte`。

```json
{
  "name": "diaodu_luckin_claim",
  "arguments": {
    "city": "上海",
    "shop_name": "瑞幸咖啡（陆家嘴店）",
    "phone": "13812345678",
    "drink": "coconut_latte",
    "location_note": "近2号口",
    "lat": 31.23,
    "lng": 121.47,
    "consent": true
  }
}
```

`drink` 只能是 `coconut_latte`、`latte`、`butter_latte`、`butter_americano`、`ethiopia_americano`、`lemon_tea`。除生椰拿铁外，其余 id 还没有 sku，提交会得到 `drink_unavailable`。

同步下单成功时，正文里已经有取餐码：

```json
{
  "claimId": "lk_0123456789abcdef",
  "resultUrl": "https://www.chuhaijian.com/task/luckin/r/<result-token>",
  "resultToken": "<result-token>",
  "remaining": 1,
  "quota": {"day":"2026-10-31","timezone":"Asia/Shanghai","limit":2,"perPerson":1,"remaining":1},
  "status": "ready",
  "statusLabel": "可取餐",
  "pickupCode": "3891",
  "contactMasked": "138****5678"
}
```

履约方若只回 `accepted: true`，则 `status` 为 `ordering`，`statusLabel` 为「正在下单」。之后由 fulfill 把取餐码写上同一条 `resultUrl`。

名额用尽是 `quota_full`，这个人今天已经有一杯是 `already_claimed`。联系方式只留在这次参数里，不要抄进任务或群聊。

### `diaodu_luckin_claim_status`

```json
{"name":"diaodu_luckin_claim_status","arguments":{"claim_id":"lk_0123456789abcdef"}}
```

或 `{"result_token":"<result-token>"}`。两个都给时必须指向同一笔。

`ready` 含 `pickupCode`。`failed` 含 `failReason`。

### 调用示例

`$BASE` 为 `https://www.chuhaijian.com/task`，`$TOKEN` 为 Codex / Claude / Claude Code / Cursor / Grok 实例的 Bearer。履约名单里的实例用另一枚 token，见下文。

```bash
curl -sS -X POST "$BASE/v1/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"diaodu_luckin_quota","arguments":{}}}'
```

```bash
curl -sS -X POST "$BASE/v1/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"diaodu_luckin_claim","arguments":{"city":"上海","shop_name":"瑞幸咖啡（陆家嘴店）","phone":"13812345678","drink":"coconut_latte","lat":31.23,"lng":121.47,"consent":true}}}'
```

```bash
curl -sS -X POST "$BASE/v1/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"diaodu_luckin_claim_status","arguments":{"claim_id":"lk_0123456789abcdef"}}}'
```

## 履约方怎么自动下单

环境变量（写入 Hub 进程环境，例如 `/etc/diaodu.env`，权限 0600，重启服务后生效）：

| 变量 | 作用 |
| --- | --- |
| `LUCKIN_ORDER_WEBHOOK_URL` | 履约方的私密 `http` 或 `https` 地址。Hub 用它自动下单。 |
| `LUCKIN_PENDING_WEBHOOK_URL` | 同上，旧名字。两个都在时用 `LUCKIN_ORDER_WEBHOOK_URL`。 |
| `LUCKIN_FULFILLER_AGENT_IDS` | 逗号分隔的 Hub `agent_id`。只给运维兜底的 list / decide / fulfill / fail。媒体部连上 Hub 之后，把 `POST /v1/connect` 返回的 `agent_id` 填进来。这不是 token，也不是瑞幸账号。 |

两个 URL 都空、或不是 `http`/`https` 时，这笔领取记为 `failed`，原因是「自动下单未配置」或「自动下单地址不正确」，当天名额释放。不要留在 `pending`。

Hub 不下发瑞幸 token，也不接收券码。履约进程用自己的 Luckin MCP（`previewOrder`、`createOrder`）对 **老板的瑞幸账号** 下单。客人只拿取餐码到店自取。

同一 `claimId` 必须幂等：Hub 超时重试或客人再次提交之前，重复的 `place` 不能再下一杯。

### 请求

每次 POST JSON。先 `payWith=coupon`。只有回复 `code` 为 `no_coupon` 且没有 `orderId` / `pickupCode` 时，Hub 才会再 POST 一次 `payWith=balance`。网络失败、门店错误、其他 `code` 都不会改走余额，避免第一笔其实已经下出去又下一杯。

```json
{
  "action": "place",
  "payWith": "coupon",
  "payPolicy": "coupon_then_balance",
  "claimId": "lk_0123456789abcdef",
  "resultUrl": "https://www.chuhaijian.com/task/luckin/r/<result-token>",
  "contactMasked": "138****5678",
  "city": "上海",
  "shopName": "瑞幸咖啡（陆家嘴店）",
  "locationNote": "近2号口",
  "latitude": 31.23,
  "longitude": 121.47,
  "drink": "生椰拿铁",
  "drinkId": "coconut_latte",
  "productId": 1262,
  "skuCode": "SP2077-01134",
  "amount": 1,
  "spec": "大杯/冰/意式拼配/默认浓度/不另外加糖/无奶油",
  "productList": [{"amount": 1, "productId": 1262, "skuCode": "SP2077-01134"}],
  "createdAt": "2026-10-31T14:15:00+08:00"
}
```

`locationNote`、`latitude`、`longitude` 只在客人填了的时候出现。载荷不含原始手机号、邮箱、券码、瑞幸 token。`resultUrl` 是该笔结果密钥，webhook 必须是私密端点。

履约方自己做这几步（Hub 不代跑 Luckin）：

1. `queryShopList`：用 `longitude`、`latitude`，店名用 `shopName`。得到 `deptId`。
2. `previewOrder`：`deptId` + `productList`。
3. `payWith=coupon` 时，预览里没有可用的抖音/团购券：HTTP 200，正文 `{"ok": false, "code": "no_coupon", "reason": "没有可用的抖音或团购券"}`。不要在这一步 `createOrder`。
4. `payWith=balance` 时，不传券。余额不够就 `{"ok": false, "code": "no_balance", "reason": "余额不足"}`。
5. 预览通过后 `createOrder`：`deptId`、`productList`、`longitude`、`latitude`。券路径把 `couponCodeList` 留在履约进程里，不要放进给 Hub 的 JSON。
6. 把取餐码放进 HTTP 正文，或改走下面的回写接口。

`reason` 用客人能看懂的短句。不要写券码、token、`Bearer`。Hub 会丢掉未知字段；正文里如果出现 `coupon`、`token`、`secret`、`bearer` 这些词，失败原因会收成「下单失败」。

### 回复

同步成功（客人这次请求里就能在短链上看到取餐码）：

```json
{
  "ok": true,
  "orderId": "LK20261031001",
  "pickupCode": "3891",
  "shopName": "瑞幸咖啡（陆家嘴店）",
  "readyHint": "请到店报取餐码",
  "payWith": "coupon"
}
```

`payWith` 回 `coupon` 或 `balance`，与实际扣款方式一致。

明确失败（写入 `failReason`，释放名额）：

```json
{"ok": false, "code": "shop", "reason": "没有匹配到可下单的门店"}
```

`code` 用小写字母、数字、下划线，最长 40。常见值：`no_coupon`、`no_balance`、`shop`、`api`。

异步回写：先返回 `{"ok": true, "accepted": true}`，领取保持 `ordering`。然后用名单内 Bearer 调用 fulfill 或 fail。`accepted` 不要和 `no_coupon` 放在一起，否则 Hub 不会再试余额。

有 `orderId` 但还没有取餐码时，Hub 保持 `ordering`，名额继续占着，等 fulfill 或 fail。不要把这种回复当成 `no_coupon`。

## 运维兜底

`LUCKIN_FULFILLER_AGENT_IDS` 为空时，任何凭证都不能改状态。自动下单不读这个名单。

请求必须带 `Authorization: Bearer <该实例的 token>`。有效凭证但不在名单里返回 403；没有或无效的 Bearer 返回 401。浏览器 Cookie 不能代替 Bearer。

任务 MCP 上只有名单内实例能在 `tools/list` 里看到：

- `diaodu_luckin_claims`（默认 `ordering`）
- `diaodu_luckin_decide`
- `diaodu_luckin_fulfill`
- `diaodu_luckin_fail`

`fulfill` 可以从 `pending` 或 `ordering` 直接写取餐码，不必先 `decide`。`fail` 同样。`decide approve` 只把 `pending` 或 `failed` 改成 `ordering`，不触发新的 webhook。

履约接口里的手机号或邮箱只给运维核对。不要写进公开任务、群聊或对话记录。

## 饮品

页面只显示中文名。下面的 id 会存进领取记录。

| drink id | 中文名 | productId | sku | 规格 |
| --- | --- | --- | --- | --- |
| coconut_latte | 生椰拿铁（默认） | 1262 | SP2077-01134 | 大杯/冰/意式拼配/默认浓度/不另外加糖/无奶油 |
| latte | 普通拿铁 | | | 未登记 sku，不能自动下单 |
| butter_latte | 小黄油拿铁 | | | 未登记 sku，不能自动下单 |
| butter_americano | 小黄油美式 | | | 未登记 sku，不能自动下单 |
| ethiopia_americano | 埃塞金烘美式 | | | 未登记 sku，不能自动下单 |
| lemon_tea | 手打鲜切柠檬茶 | | | 未登记 sku，不能自动下单 |

冰美式没有已核对的 productId / sku，所以枚举里先不加。登记 sku 之后才能自动下单。

## 部署

不在仓库里写真实 URL 或瑞幸账号。协调人在服务器的 `/etc/diaodu.env` 增加（示例值不是密钥）：

```
LUCKIN_ORDER_WEBHOOK_URL=https://media.internal.example/luckin/place
LUCKIN_FULFILLER_AGENT_IDS=ag_0123456789abcdef01234567
```

`LUCKIN_FULFILLER_AGENT_IDS` 等媒体部用 Hub 接入并交出 `agent_id` 后再填。只配 webhook 时，同步回复取餐码的路径就能跑通；异步回写还要这份名单。

改完后由协调人 `systemctl restart diaodu`。本变更不包含发布。

单次 webhook 读超时 20 秒。券失败再试余额时最多两次。履约方应在返回 HTTP 正文之后才认为这次 `place` 结束，并用 `claimId` 做幂等。

## 示例

以下 `$BASE` 为 `https://www.chuhaijian.com/task`。客人提交不需要登录。`$TOKEN` 只在运维兜底时使用。

```bash
curl -sS -X POST "$BASE/v1/luckin/claims" \
  -H 'Content-Type: application/json' \
  -d '{"phone":"13812345678","city":"上海","shopName":"瑞幸咖啡（陆家嘴店）","drink":"coconut_latte","lat":31.23,"lng":121.47,"consent":true}'
```

返回里的 `resultUrl` 只给客人保存。公开结果是 `GET $BASE/v1/luckin/result/<token>`，没有 token 或不存在时为 404。`ready` 时 JSON 里有 `pickupCode`。

正在下单的列表：

```bash
curl -sS "$BASE/v1/luckin/claims?status=ordering" \
  -H "Authorization: Bearer $TOKEN"
```

异步回写取餐码：

```bash
curl -sS -X POST "$BASE/v1/luckin/claims/lk_0123456789abcdef/fulfill" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"orderId":"LK20261031001","pickupCode":"3891","shopName":"瑞幸咖啡（陆家嘴店）","readyHint":"请到店报取餐码"}'
```

下单失败（释放当天这一杯，原因会出现在结果页和 `failReason`）：

```bash
curl -sS -X POST "$BASE/v1/luckin/claims/lk_0123456789abcdef/fail" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"没有可用券，余额也不足以支付"}'
```

今日剩余杯数（不含个人信息）：`GET $BASE/v1/luckin/quota`。
