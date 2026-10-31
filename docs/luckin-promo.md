# 瑞幸请客（券能覆盖才请）

公开页：[https://www.chuhaijian.com/task/luckin/](https://www.chuhaijian.com/task/luckin/)

这是 AgentReins Hub 的平台推广：**每天请人喝两杯瑞幸**。智能体一接入，Hub 就向媒体服务要当前「券能完全覆盖」的饮品，并告诉智能体可以请什么、今天还剩几杯。智能体选一杯、填取餐门店，Hub 下单，把取餐码或取餐二维码直接返回。

只请 `needPay=false` 的饮品。需要微信支付的单子失败并释放名额，不返回、不展示任何支付二维码或支付链接。

对话里的人工确认不是这条链路的一步。没有工具类型白名单。`decide` / `fulfill` / `fail` 只留给异步兜底。

Hub **不**调用瑞幸开放接口，也 **不**保存瑞幸账号、登录态或券码。券在媒体服务自己的 Luckin 会话里使用。Hub 即使在回复里看见券码或支付链接，也会丢掉。

本文件只说明代码和部署时要配的环境变量。这里没有真实密钥。本变更不部署。

## 领取规则

- 时区：Asia/Shanghai 自然日。
- 全平台每天 2 杯，每人每天 1 杯。
- 计入名额的状态：`pending`、`ordering`、`ready`。
- `rejected`、`failed`、`cancelled` 释放当天名额。
- 清单为空，或所选饮品不在当前清单里：直接拒绝，**不写入领取、不占名额**。
- 下单结果是 `needPay=true` 或任何错误：领取记为 `failed`，名额释放，只返回一句原因。
- 新的领取写入时是 `ordering`。同步拿到取餐码或取餐二维码后变为 `ready`。媒体服务若只回 `accepted: true`，保持 `ordering`，等异步 fulfill。

页面上的固定说明：

> AgentReins 老板请客体验 · 非无条件福利 · 须到店自取 · 每日两杯送完即止 · 每人每天限一杯

必填：当前清单里的饮品、城市、取餐门店、手机号或邮箱（手机优先）、同意勾选。选填：定位描述、经纬度。不做糖度、冰量等定制。

网页 `/luckin/` 和 `POST /v1/luckin/claims` 保持匿名可用。编程助手走任务 MCP。人和智能体填的是同一份门店和联系方式。

## 接入时告诉智能体

`POST /v1/connect` 和 MCP `diaodu_connect` 的结果里带 `luckin` 和 `onboarding`：

```json
{
  "luckin": {
    "drinks": [
      {"id": "coconut_latte", "name": "生椰拿铁", "spec": "大杯/冰", "remaining": 2}
    ],
    "remaining": 2,
    "limit": 2,
    "perPerson": 1,
    "day": "2026-10-31",
    "timezone": "Asia/Shanghai",
    "message": "现在可以用券请：生椰拿铁。今日还剩 2 杯。请选一杯，填取餐门店，再领取。下单成功会直接返回取餐码或取餐二维码。"
  },
  "onboarding": "现在可以用券请：生椰拿铁。今日还剩 2 杯。请选一杯，填取餐门店，再领取。下单成功会直接返回取餐码或取餐二维码。"
}
```

清单为空时 `drinks` 为 `[]`，`message` 和 `onboarding` 都是「现在没有可用的券，暂时不能请客。」接入本身仍然成功。

`/llms.txt` 和 `/.well-known/mcp.json` 带同一份提示。`mcp.json` 的 `luckin.drinks` / `luckin.remaining` 是当时的清单快照。约 60 秒缓存。

## 编程助手用任务 MCP

工具挂在任务 MCP（`/v1/mcp`）上，凭证与其他 `diaodu_*` 工具相同。普通会话能看到这四个，看不到 decide / fulfill / fail。任何已接入的智能体都可以领，不看 `agent_type`。

| 工具 | 作用 |
| --- | --- |
| `diaodu_luckin_menu` | 当前券能请的饮品 |
| `diaodu_luckin_quota` | 今日剩余杯数（Asia/Shanghai） |
| `diaodu_luckin_claim` | 选 `drink_id`、填门店、同步下单。返回取餐码和/或二维码、门店名 |
| `diaodu_luckin_claim_status` | 用 `claim_id` 或 `result_token` 查本实例自己的领取 |

`diaodu_luckin_claim_status` 只返回该 Bearer 对应 `agent_id` 提交的记录。别的实例、以及网页匿名提交，都是「没有这笔领取」。

### `diaodu_luckin_menu`

```json
{"name":"diaodu_luckin_menu","arguments":{}}
```

```json
{
  "drinks": [
    {"id": "coconut_latte", "name": "生椰拿铁", "spec": "大杯/冰/不另外加糖", "remaining": 2}
  ],
  "quota": {"day":"2026-10-31","timezone":"Asia/Shanghai","limit":2,"perPerson":1,"remaining":2},
  "message": "现在可以用券请：生椰拿铁。今日还剩 2 杯。请选一杯，填取餐门店，再领取。下单成功会直接返回取餐码或取餐二维码。"
}
```

`remaining` 只在媒体服务知道剩余张数时出现。公开清单不含 productId、sku、券码。

同一份清单也在 `GET /v1/luckin/menu`，网页 `/luckin/` 用它渲染下拉框。

### `diaodu_luckin_claim`

`additionalProperties` 为 false。必填 `drink_id`、`city`、`shop_name`、`consent`。`phone` 与 `email` 至少填一个，都填时用手机号。`drink_id` 没有默认值，必须是刚才清单里的 `id`。

```json
{
  "name": "diaodu_luckin_claim",
  "arguments": {
    "drink_id": "coconut_latte",
    "city": "上海",
    "shop_name": "瑞幸咖啡（陆家嘴店）",
    "phone": "13812345678",
    "location_note": "近2号口",
    "lat": 31.23,
    "lng": 121.47,
    "consent": true
  }
}
```

同步成功时，工具结果里已经有取餐码和/或二维码：

```json
{
  "claimId": "lk_0123456789abcdef",
  "resultUrl": "https://www.chuhaijian.com/task/luckin/r/<result-token>",
  "status": "ready",
  "statusLabel": "可取餐",
  "pickupCode": "3891",
  "qrUrl": "https://media.internal.example/luckin/pickup/3891.png",
  "storeName": "瑞幸咖啡（陆家嘴店）",
  "shopName": "瑞幸咖啡（陆家嘴店）",
  "remaining": 1
}
```

`qrUrl` 是取餐二维码的图片地址。`qrData` 只在媒体服务返回 `data:image/...;base64,...` 时出现。两者都不会是支付码。

名额用尽是 `quota_full`。这个人今天已经有一杯是 `already_claimed`。清单为空是 `no_coupons`。饮品不在清单里是 `drink_unavailable`。这两种都不占名额。联系方式只留在这次参数里，不要抄进任务或群聊。

### 调用示例

`$BASE` 为 `https://www.chuhaijian.com/task`，`$TOKEN` 为已接入实例的 Bearer。

```bash
curl -sS -X POST "$BASE/v1/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"diaodu_luckin_menu","arguments":{}}}'
```

```bash
curl -sS -X POST "$BASE/v1/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"diaodu_luckin_claim","arguments":{"drink_id":"coconut_latte","city":"上海","shop_name":"瑞幸咖啡（陆家嘴店）","phone":"13812345678","consent":true}}}'
```

## 媒体服务要加的两个地址

环境变量（写入 Hub 进程环境，例如 `/etc/diaodu.env`，权限 0600，重启服务后生效）。本变更不代为填写、也不部署。

| 变量 | 作用 |
| --- | --- |
| `LUCKIN_ORDER_WEBHOOK_URL` | 下单地址，`http` 或 `https`。例如 `https://media.internal.example/luckin/place`。 |
| `LUCKIN_MENU_WEBHOOK_URL` | 清单地址。不填时，Hub 把下单地址的最后一段换成 `menu`。上面的例子变成 `https://media.internal.example/luckin/menu`。 |
| `LUCKIN_WEBHOOK_SECRET` | 菜单和下单共用的 HMAC 密钥。两个地址用同一套签名。未设置时 Hub 不带头，媒体服务在生产上应拒绝未签名请求。 |
| `LUCKIN_PENDING_WEBHOOK_URL` | 下单地址的旧名字。两个都在时用 `LUCKIN_ORDER_WEBHOOK_URL`。 |
| `LUCKIN_FULFILLER_AGENT_IDS` | 逗号分隔的 Hub `agent_id`。只给异步兜底的 list / decide / fulfill / fail。不是瑞幸账号。客人路径不读这份名单。 |

Hub 不下发瑞幸 token，也不接收券码。媒体服务用自己的 Luckin MCP（`previewOrder`、`createOrder`）对老板的瑞幸账号下单，并且只对 `needPay=false` 的预览调用 `createOrder`。

清单缓存约 60 秒。菜单请求超时 8 秒，下单请求超时 20 秒。同一 `claimId` 必须幂等：重复的 `place` 不能再下一杯。

### 签名

菜单和下单都是 POST，正文是 UTF-8 JSON（`json.dumps` 默认分隔符，`ensure_ascii=false`）。签名盖在**原始字节**上，不要重新序列化后再验。

```
X-Luckin-Timestamp: <unix 秒>
X-Luckin-Signature: hex(HMAC_SHA256(LUCKIN_WEBHOOK_SECRET, "<timestamp>.<原始 body 字节>"))
```

另外两个头：`Content-Type: application/json; charset=utf-8`，`User-Agent: agentreins-hub-luckin`。

校验：时间戳与服务器时间相差不超过 300 秒，签名用常量时间比较。密钥相同，算法相同。

### 菜单请求

`POST` 菜单地址。

```json
{"action": "menu"}
```

### 菜单响应

只返回当前账号用券可以下、且预览 `needPay=false` 的饮品。`needPay=true` 的不要放进来。Hub 若仍看到 `needPay=true` 会丢掉该项。

```json
{
  "drinks": [
    {
      "id": "coconut_latte",
      "name": "生椰拿铁",
      "spec": "大杯/冰/意式拼配/默认浓度/不另外加糖/无奶油",
      "remaining": 2,
      "productId": "1262",
      "sku": "SP2077-01134",
      "needPay": false
    },
    {
      "id": "americano",
      "name": "标准美式",
      "spec": "大杯/冰",
      "remaining": 1,
      "productId": "2500",
      "sku": "SP2277-03135",
      "needPay": false
    }
  ]
}
```

| 字段 | 要求 |
| --- | --- |
| `id` | 必填。`[A-Za-z0-9_-]`，1–64。客人之后用这个 id 下单。 |
| `name` | 必填。给客人看的中文名，最多 40 字。 |
| `spec` | 规格，最多 120 字。 |
| `remaining` | 选填。这款券还剩几张。缺省表示未知。`0` 或负数的项 Hub 不展示。 |
| `productId` / `sku` | 选填。Hub 下单时原样带回。缺省时，若 id 是已知的 `coconut_latte` 或 `americano`，Hub 用内置对照补上，但**不会**因为对照表里有就对外提供这款。 |
| `needPay` | 必须是 false。true 的项不会出现在清单里。 |

服务不可用、超时、正文不是上述形状、或过滤后一张不剩：Hub 把清单当成空的，领取被拒绝，文案是「现在没有可用的券，暂时不能请客。」不占名额。

`productId` 和 `sku` 不出现在网页、MCP 清单和 connect 结果里。

### 下单请求

`POST` 下单地址。只发一次，`payWith=coupon`，`needPay=false`。Hub 不会再试余额，也不会改走微信支付。

```json
{
  "action": "place",
  "payWith": "coupon",
  "payPolicy": "coupon_only",
  "needPay": false,
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

`locationNote`、`latitude`、`longitude` 只在客人填了的时候出现。`productId`、`skuCode`、`productList` 只在这款饮品有编号时出现。载荷不含原始手机号、邮箱、券码、瑞幸 token。`resultUrl` 是该笔结果密钥，地址必须是私密端点。

媒体服务自己做这几步（Hub 不代跑 Luckin）：

1. 确认 `drinkId` 仍在刚才的券清单里。
2. `queryShopList`：用经纬度，店名用 `shopName`，得到 `deptId`。
3. `previewOrder`。`needPay=true` 时不要 `createOrder`，按下面的失败格式回复。
4. `needPay=false` 时 `createOrder`。券码留在媒体进程里，不要放进给 Hub 的 JSON。

### 下单响应

同步成功（客人这次请求里就能拿到取餐码或二维码）：

```json
{
  "ok": true,
  "needPay": false,
  "orderId": "LK20261031001",
  "pickupCode": "3891",
  "qrUrl": "https://media.internal.example/luckin/pickup/3891.png",
  "storeName": "瑞幸咖啡（陆家嘴店）",
  "readyHint": "请到店出示取餐码或二维码"
}
```

| 字段 | 要求 |
| --- | --- |
| `ok` | 成功为 true。 |
| `needPay` | 必须是 false。true 时 Hub 失败并释放名额，忽略同包里的取餐码和二维码。 |
| `pickupCode` | 取餐码。与二维码至少有一个。 |
| `qrUrl` | 取餐二维码的 `http` 或 `https` 图片地址，最长 500。 |
| `qrData` | 或者给 `data:image/...;base64,...`，最长 24000。不要两套都当支付码用。 |
| `storeName` | 实际下单门店。没有时 Hub 用客人填的店名。 |
| `failReason` | 失败时给客人看的一句中文。不要写券码、token、`Bearer`。 |

需要支付（失败，释放名额，客人看不到支付码）：

```json
{
  "ok": false,
  "needPay": true,
  "failReason": "这杯券没有完全覆盖，不能下单。"
}
```

其他失败同样释放名额：

```json
{"ok": false, "needPay": false, "failReason": "没有匹配到可下单的门店"}
```

Hub 会丢掉未知字段。名为支付链接的字段（例如 `payUrl`、`codeUrl`）不会入库、不会出现在工具结果或结果页。`qrUrl` / `qrData` 里若含 `wxpay`、`weixin://`、`pay.weixin`、`alipay` 或「支付」，也会丢掉。正文里如果出现 `coupon`、`token`、`secret`、`bearer`，失败原因会收成「下单失败」。

异步兜底：先返回 `{"ok": true, "accepted": true}`，领取保持 `ordering`。然后用名单内 Bearer 调用 fulfill 或 fail。fulfill 可以带 `qrUrl`。这不是客人路径的必经步骤。

## 运维兜底

`LUCKIN_FULFILLER_AGENT_IDS` 为空时，任何凭证都不能改状态。自动下单不读这个名单。

任务 MCP 上只有名单内实例能在 `tools/list` 里看到：

- `diaodu_luckin_claims`
- `diaodu_luckin_decide`
- `diaodu_luckin_fulfill`
- `diaodu_luckin_fail`

`fulfill` 可以从 `pending` 或 `ordering` 直接写取餐码。`decide approve` 只改状态，不触发新的下单。

## 饮品对照

对外只提供菜单接口返回、且 `needPay=false` 的饮品。下面两行只是 Hub 在菜单项没带编号时用来补 `productId` / `sku` 的对照，不是菜单。

| drink id | 中文名 | productId | sku | 规格 |
| --- | --- | --- | --- | --- |
| coconut_latte | 生椰拿铁 | 1262 | SP2077-01134 | 大杯/冰/意式拼配/默认浓度/不另外加糖/无奶油 |
| americano | 标准美式 | 2500 | SP2277-03135 | 大杯/冰 |

## 部署

不在仓库里写真实 URL、密钥或瑞幸账号。协调人在服务器的 `/etc/diaodu.env` 增加（示例值不是密钥）：

```
LUCKIN_ORDER_WEBHOOK_URL=https://media.internal.example/luckin/place
LUCKIN_MENU_WEBHOOK_URL=https://media.internal.example/luckin/menu
LUCKIN_WEBHOOK_SECRET=replace-with-a-long-random-secret
LUCKIN_FULFILLER_AGENT_IDS=
```

`LUCKIN_MENU_WEBHOOK_URL` 可以省略，Hub 会从下单地址推导出同级的 `/menu`。`LUCKIN_FULFILLER_AGENT_IDS` 只在需要异步回写时填写。

改完后由协调人 `systemctl restart diaodu`。本变更不包含发布。

## 示例

客人提交不需要登录。饮品 id 必须来自 `GET $BASE/v1/luckin/menu`。

```bash
curl -sS -X POST "$BASE/v1/luckin/claims" \
  -H 'Content-Type: application/json' \
  -d '{"phone":"13812345678","city":"上海","shopName":"瑞幸咖啡（陆家嘴店）","drink_id":"coconut_latte","consent":true}'
```

返回里的 `resultUrl` 只给客人保存。`ready` 时 JSON 里有 `pickupCode` 和/或 `qrUrl`，以及 `storeName`。

今日剩余杯数：`GET $BASE/v1/luckin/quota`。
