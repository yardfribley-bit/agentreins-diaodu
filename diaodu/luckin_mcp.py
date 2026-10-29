"""Fulfiller MCP tools. Same rules as the REST API; not a Luckin client.

Only an agent id listed in LUCKIN_FULFILLER_AGENT_IDS can call these. The
tools return the claimant contact so the media team can place the order
elsewhere. Do not copy that contact into a public task, chat, or transcript.
"""

from diaodu.identity import AuthError
from diaodu.luckin import STATUSES, is_fulfiller

S = {"type": "string"}
TOOLS = [
    (
        "luckin_claims",
        "列出瑞幸请客排队。仅 LUCKIN_FULFILLER_AGENT_IDS 中的实例可调用。"
        "返回的联系方式只用于下单，禁止写入公开任务、群聊或对话记录。Hub 不下单。",
        {"status": {"type": "string", "enum": list(STATUSES)}},
        [],
    ),
    (
        "luckin_decide",
        "人工确认瑞幸领取。对话里说「通过/下单 claimId」之后，approve 把 pending 或 failed 变为 ordering；"
        "failed 拉回仍是同一杯，不新占名额。reject 释放当日名额。Hub 不下单。",
        {
            "claim_id": S,
            "action": {"type": "string", "enum": ["approve", "reject"]},
            "note": S,
        },
        ["claim_id", "action"],
    ),
    (
        "luckin_fulfill",
        "下单成功后写回取餐码，状态变为 ready。联系方式不要写入公开渠道。Hub 不下单。",
        {
            "claim_id": S,
            "order_id": S,
            "pickup_code": S,
            "shop_name": S,
            "ready_hint": S,
            "raw_note": S,
        },
        ["claim_id", "order_id", "pickup_code"],
    ),
    (
        "luckin_fail",
        "下单失败并释放当日名额。之后可用 luckin_decide approve 把同一 claimId 拉回 ordering。Hub 不下单。",
        {"claim_id": S, "reason": S},
        ["claim_id", "reason"],
    ),
]


def call(name, args, actor, luckin, identity):
    if not actor or actor.get("kind") != "agent":
        raise AuthError("需要 Bearer 履约凭证", 401)
    if identity is not None:
        with identity.db() as db:
            row = db.execute(
                "SELECT revoked FROM agents WHERE id=?",
                (actor.get("agent_id"),),
            ).fetchone()
        if not row or row[0]:
            raise AuthError("实例凭据已撤销", 401)
    if not is_fulfiller(actor):
        raise AuthError("此实例不在 LUCKIN_FULFILLER_AGENT_IDS 允许名单", 403)
    if name == "luckin_claims":
        return {"claims": luckin.list_claims(args.get("status") or "pending")}
    if name == "luckin_decide":
        return luckin.decide(args["claim_id"], args["action"], args.get("note") or "")
    if name == "luckin_fulfill":
        return luckin.fulfill(args["claim_id"], {
            "orderId": args["order_id"],
            "pickupCode": args["pickup_code"],
            "shopName": args.get("shop_name") or "",
            "readyHint": args.get("ready_hint") or "",
            "rawNote": args.get("raw_note") or "",
        })
    if name == "luckin_fail":
        return luckin.fail(args["claim_id"], args["reason"])
    raise ValueError("未知工具")
