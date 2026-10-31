"""Luckin MCP tools.

Claim tools use the same Bearer as the other diaodu_* task tools:

- luckin_menu, luckin_quota, luckin_claim, luckin_claim_status are listed for
  every task MCP session. The menu is the drinks a coupon can fully cover.
  A claim picks one of those ids, a store, and a phone, then waits for the
  pickup code or QR. There is no agent-type whitelist and no chat confirm.

- luckin_claims, luckin_decide, luckin_fulfill, luckin_fail stay limited to
  LUCKIN_FULFILLER_AGENT_IDS. They are the async fallback. The contact on
  those replies is for the operator, not for a public task, chat, or transcript.
"""

from diaodu.identity import AuthError
from diaodu.luckin import (
    STATUSES,
    is_fulfiller,
)

S = {"type": "string"}


def _described(spec, description):
    merged = dict(spec)
    merged["description"] = description
    return merged


CLAIM_TOOLS = [
    (
        "luckin_menu",
        "当前可以用券请的饮品（needPay 为 false，券能完全覆盖）。"
        "每项有 id、name、spec，知道剩余张数时带 remaining。约 60 秒缓存。"
        "清单为空表示现在没有可用的券，此时不要调用 diaodu_luckin_claim。"
        "接入时 connect 的结果里也会带上同一份清单和今日剩余杯数。",
        {},
        [],
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    (
        "luckin_quota",
        "今日瑞幸请客还剩几杯（Asia/Shanghai 自然日）。全平台每天 2 杯，每人每天 1 杯。"
        "计入 pending、ordering、ready。能请的饮品看 diaodu_luckin_menu，不看固定菜单。",
        {},
        [],
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    (
        "luckin_claim",
        "用券请一杯瑞幸。使用与其他 diaodu_* 工具相同的 Bearer。任何已接入的智能体都可以调用，没有工具类型白名单。"
        "drink_id 必须来自 diaodu_luckin_menu。必填城市、店名、手机号或邮箱、consent=true。定位描述和经纬度选填。"
        "不在当前清单里的饮品直接拒绝，不占今日名额。清单为空时拒绝，原因是现在没有可用的券。"
        "然后同步等待下单结果。成功时结果里直接有 pickupCode 和/或 qrUrl（取餐二维码）以及 storeName。"
        "needPay 或任何错误会失败并释放今日名额，只返回一句原因，不会给出支付二维码或支付链接。"
        "联系方式不要写入任务、群聊或成果。不需要人工确认。",
        {
            "drink_id": _described(S, "饮品 id，必须是 diaodu_luckin_menu 返回的 id。"),
            "city": _described(S, "城市，1–40 字。例如上海。"),
            "shop_name": _described(S, "取餐门店，1–80 字。例如瑞幸咖啡（陆家嘴店）。"),
            "phone": _described(S, "中国大陆手机号。与 email 至少填一个；两者都填时使用手机号。"),
            "email": _described(S, "邮箱。没有手机号时填写。"),
            "location_note": _described(S, "选填定位描述，最多 120 字。"),
            "lat": {"type": "number", "description": "选填纬度。与 lng 同时填写，或都不填。"},
            "lng": {"type": "number", "description": "选填经度。与 lat 同时填写，或都不填。"},
            "consent": {
                "type": "boolean",
                "description": "必须为 true。确认到店自取、每日限额，且这不是无条件福利。",
            },
        },
        ["drink_id", "city", "shop_name", "consent"],
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    ),
    (
        "luckin_claim_status",
        "查看本实例用 diaodu_luckin_claim 提交的领取。claim_id 或 result_token 至少填一个。"
        "只能看自己的。别人的领取、以及网页匿名提交，都返回没有这笔领取。"
        "下单完成后 status 为 ready，并带 pickupCode 和/或 qrUrl，以及 storeName。"
        "失败为 failed 并带 failReason，不会带支付二维码。中间状态是 ordering（正在下单）。",
        {
            "claim_id": _described(S, "lk_ 开头的领取编号。"),
            "result_token": _described(S, "本实例 diaodu_luckin_claim 返回的 resultUrl 里的 token。"),
        },
        [],
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
]

TOOLS = [
    (
        "luckin_claims",
        "列出瑞幸请客。默认 status=ordering（正在自动下单）。仅 LUCKIN_FULFILLER_AGENT_IDS 中的实例可调用。"
        "返回的联系方式只给运维兜底，禁止写入公开任务、群聊或对话记录。",
        {"status": {"type": "string", "enum": list(STATUSES)}},
        [],
    ),
    (
        "luckin_decide",
        "运维兜底，不是领取必经步骤。approve 把 pending 或 failed 变为 ordering；"
        "failed 拉回仍是同一杯，不新占名额，也不会再次自动下单。reject 释放当日名额。"
        "仅 LUCKIN_FULFILLER_AGENT_IDS 中的实例可调用。",
        {
            "claim_id": S,
            "action": {"type": "string", "enum": ["approve", "reject"]},
            "note": S,
        },
        ["claim_id", "action"],
    ),
    (
        "luckin_fulfill",
        "把取餐码写回结果短链，状态变为 ready。pending 或 ordering 都可以写，不必先 decide。"
        "可以同时写 qr_url（取餐二维码图片地址）或 qr_data（data:image）。不要写入支付二维码或支付链接。"
        "联系方式不要写入公开渠道。仅 LUCKIN_FULFILLER_AGENT_IDS 中的实例可调用。",
        {
            "claim_id": S,
            "order_id": S,
            "pickup_code": S,
            "shop_name": S,
            "store_name": S,
            "qr_url": _described(S, "取餐二维码的 http(s) 图片地址。不要填支付二维码。"),
            "qr_data": _described(S, "取餐二维码的 data:image/...;base64,... 。不要填支付二维码。"),
            "ready_hint": S,
            "raw_note": S,
        },
        ["claim_id", "order_id", "pickup_code"],
    ),
    (
        "luckin_fail",
        "下单失败：写入 reason，并释放当日名额。不要把领取留在 pending。"
        "之后可用 luckin_decide approve 把同一 claimId 拉回 ordering（不自动再下单）。"
        "仅 LUCKIN_FULFILLER_AGENT_IDS 中的实例可调用。",
        {"claim_id": S, "reason": S},
        ["claim_id", "reason"],
    ),
]

CLAIM_NAMES = {item[0] for item in CLAIM_TOOLS}


def _require_agent(actor, identity):
    if not actor or actor.get("kind") != "agent":
        raise AuthError("先调用 diaodu_connect 或提供 Authorization: Bearer。瑞幸领取与其他 diaodu_* 工具使用同一枚凭证。", 401)
    if identity is not None:
        with identity.db() as db:
            row = db.execute(
                "SELECT revoked FROM agents WHERE id=?",
                (actor.get("agent_id"),),
            ).fetchone()
        if not row or row[0]:
            raise AuthError("实例凭据已撤销", 401)


def _claim_body(args):
    body = {
        "city": args["city"],
        "shopName": args["shop_name"],
        "drink_id": args["drink_id"],
        "consent": args["consent"],
    }
    if args.get("phone"):
        body["phone"] = args["phone"]
    if args.get("email"):
        body["email"] = args["email"]
    if args.get("location_note"):
        body["locationNote"] = args["location_note"]
    if "lat" in args:
        body["lat"] = args["lat"]
    if "lng" in args:
        body["lng"] = args["lng"]
    return body


def _public_call(name, args, actor, luckin, origin, client_ip):
    if name == "luckin_menu":
        if client_ip:
            luckin.limit("read", client_ip, luckin.read_ip_limit)
        offer = luckin.offer()
        return {"drinks": offer["drinks"], "quota": luckin.quota(), "message": offer["message"]}
    if name == "luckin_quota":
        if client_ip:
            luckin.limit("read", client_ip, luckin.read_ip_limit)
        return luckin.quota()
    if name == "luckin_claim_status":
        if client_ip:
            luckin.limit("read", client_ip, luckin.read_ip_limit)
        return luckin.claim_for_agent(
            actor.get("agent_id") or "",
            claim_id=args.get("claim_id") or "",
            result_token=args.get("result_token") or "",
            origin=origin,
        )
    if name == "luckin_claim":
        if not args.get("phone") and not args.get("email"):
            raise ValueError("请填写手机号或邮箱")
        if client_ip:
            luckin.limit("claim-ip", client_ip, luckin.claim_ip_limit)
        shown = luckin.create(_claim_body(args), origin, agent_id=actor["agent_id"])
        quota = luckin.quota()
        shown["quota"] = quota
        shown["remaining"] = quota["remaining"]
        return shown
    raise ValueError("未知工具")


def call(name, args, actor, luckin, identity, origin="", client_ip=None):
    _require_agent(actor, identity)
    if name in CLAIM_NAMES:
        return _public_call(name, args, actor, luckin, origin, client_ip)
    if not is_fulfiller(actor):
        raise AuthError("此实例不在 LUCKIN_FULFILLER_AGENT_IDS 允许名单", 403)
    if name == "luckin_claims":
        return {"claims": luckin.list_claims(args.get("status") or "ordering")}
    if name == "luckin_decide":
        return luckin.decide(args["claim_id"], args["action"], args.get("note") or "")
    if name == "luckin_fulfill":
        return luckin.fulfill(args["claim_id"], {
            "orderId": args["order_id"],
            "pickupCode": args["pickup_code"],
            "shopName": args.get("shop_name") or "",
            "storeName": args.get("store_name") or "",
            "qrUrl": args.get("qr_url") or "",
            "qrData": args.get("qr_data") or "",
            "readyHint": args.get("ready_hint") or "",
            "rawNote": args.get("raw_note") or "",
        })
    if name == "luckin_fail":
        return luckin.fail(args["claim_id"], args["reason"])
    raise ValueError("未知工具")
