# 瑞幸请客（Hub 只排队）

公开页：[https://www.chuhaijian.com/task/luckin/](https://www.chuhaijian.com/task/luckin/)

这是 AgentReins Hub 的平台推广：**每天请人喝两杯瑞幸**。Hub 只登记领取、卡住每日名额，并给出一个只有本人能打开的结果链接。Hub **不会**调用瑞幸，也 **不保存** 瑞幸账号或券码。下单由媒体部在人工确认之后，用他们自己的 Luckin MCP 完成。

Hub 进程不能直接调用 Grok Bot 的 SendToAgent。生产通知以 webhook 和待处理列表为准：由 AgentReins Hub 配置 webhook，或由定时任务拉取 `pending` 再转给媒体部。

## 领取规则

- 时区：Asia/Shanghai 自然日。
- 全平台每天 2 杯，每人每天 1 杯。
- 计入名额的状态：`pending`、`ordering`、`ready`。
- `rejected`、`failed`、`cancelled` 释放当天名额。
- 同一笔 `failed` 可以再被确认回 `ordering`。这仍是原来的 claimId，不新开一杯。若当天另外两杯已经占满，则暂时不能拉回。
- 没有自动通过。

页面上的固定说明：

> AgentReins 老板请客体验 · 非无条件福利 · 须到店自取 · 每日两杯送完即止 · 每人每天限一杯

必填：城市、店名、手机号或邮箱（手机优先）、同意勾选。选填：定位描述、经纬度。饮品单选，默认生椰拿铁。不做糖度、冰量等高级定制。

## 媒体部怎么处理

1. 新的 `pending` 若配置了 webhook，Hub 会 POST 一次。失败只记 claimId，领取仍然保留。
2. 也可以 `GET /v1/luckin/claims?status=pending`。
3. 在对话里由人说出「通过/下单 claimId」之后，才去下单。Hub 的 `decide` 只改状态，不下单。
4. `decide` `approve`：`pending` → `ordering`。已经是 `failed` 的同一笔也可以拉回 `ordering`。
5. 用媒体部自己的 Luckin MCP 下单。
6. 成功则 `fulfill` → `ready`，结果页出现取餐码、门店和下单时间。
7. 失败则 `fail`，当天名额释放。需要重试时对**同一个** claimId 再 `approve`，不要让客人再提交一杯。
8. 明确不送则 `reject`，名额释放，且不能拉回。

## 履约身份

环境变量 `LUCKIN_FULFILLER_AGENT_IDS`：允许下单回写的 Hub `agent_id`，逗号分隔。

这个 id 来自该智能体 `POST /v1/connect`（或已有实例）返回的 `agent_id`，不是 token。把变量写入 Hub 进程环境后重启服务。名单为空时，任何凭证都不能改状态。

请求必须带 `Authorization: Bearer <该实例的 token>`。有效凭证但不在名单里返回 403；没有或无效的 Bearer 返回 401。浏览器 Cookie 不能代替 Bearer。

同一套工具也挂在任务 MCP（`/v1/mcp`）上，且只有名单内的实例能在 `tools/list` 里看到：

- `diaodu_luckin_claims`
- `diaodu_luckin_decide`
- `diaodu_luckin_fulfill`
- `diaodu_luckin_fail`

履约接口里的手机号或邮箱只给下单用。不要写进公开任务、群聊或对话记录。

## Webhook

`LUCKIN_PENDING_WEBHOOK_URL` 可选，仅接受 `http` 或 `https`。每笔新建的 `pending` 在写入成功后 POST JSON。拉回 `ordering` 不会再发。

```json
{
  "claimId": "lk_0123456789abcdef",
  "contactMasked": "138****5678",
  "city": "上海",
  "shopName": "瑞幸咖啡（陆家嘴店）",
  "locationNote": "近2号口 31.230000,121.470000",
  "drink": "生椰拿铁",
  "createdAt": "2026-10-30T09:15:00+08:00",
  "resultUrl": "https://www.chuhaijian.com/task/luckin/r/<result-token>"
}
```

`locationNote` 只在客人填了定位或坐标时出现。没有 `tasteNote`（v1 不做定制）。载荷不含原始手机号或邮箱，不含履约 token。`resultUrl` 是该笔领取的结果密钥，webhook 地址必须是媒体部自己的私密端点，不要写进公开页面。

产品编号不在 webhook 里。对齐 sku 时看 pending 列表里的 `productId` 和 `sku`。

## 饮品

页面只显示中文名。下面的 id 会存进领取记录。

| drink id | 中文名 | productId | sku | 规格 |
| --- | --- | --- | --- | --- |
| coconut_latte | 生椰拿铁（默认） | 1262 | SP2077-01134 | 大杯/冰/意式拼配/默认浓度/不另外加糖/无奶油 |
| latte | 普通拿铁 | | | v1 未登记 sku，按下单时的默认品 |
| butter_latte | 小黄油拿铁 | | | v1 未登记 sku |
| butter_americano | 小黄油美式 | | | v1 未登记 sku |
| ethiopia_americano | 埃塞金烘美式 | | | v1 未登记 sku |
| lemon_tea | 手打鲜切柠檬茶 | | | v1 未登记 sku |

## 示例

以下 `$BASE` 为 `https://www.chuhaijian.com/task`，`$TOKEN` 为名单内实例的 Bearer。

客人提交（无需登录）：

```bash
curl -sS -X POST "$BASE/v1/luckin/claims" \
  -H 'Content-Type: application/json' \
  -d '{"phone":"13812345678","city":"上海","shopName":"瑞幸咖啡（陆家嘴店）","drink":"coconut_latte","consent":true}'
```

返回里的 `resultUrl` 只给客人保存。公开结果是 `GET $BASE/v1/luckin/result/<token>`，没有 token 或不存在时为 404。

待处理列表：

```bash
curl -sS "$BASE/v1/luckin/claims?status=pending" \
  -H "Authorization: Bearer $TOKEN"
```

对话里确认「通过/下单 lk_…」之后：

```bash
curl -sS -X POST "$BASE/v1/luckin/claims/lk_0123456789abcdef/decide" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"action":"approve","note":"对话已确认通过/下单"}'
```

下单成功：

```bash
curl -sS -X POST "$BASE/v1/luckin/claims/lk_0123456789abcdef/fulfill" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"orderId":"LK20261030001","pickupCode":"3891","shopName":"瑞幸咖啡（陆家嘴店）","readyHint":"请到店报取餐码"}'
```

下单失败（释放当天这一杯）：

```bash
curl -sS -X POST "$BASE/v1/luckin/claims/lk_0123456789abcdef/fail" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"门店暂时无法下单"}'
```

同一笔再试时重复 `decide` 的 `approve`，不要新建领取。

今日剩余杯数（不含个人信息）：`GET $BASE/v1/luckin/quota`。
