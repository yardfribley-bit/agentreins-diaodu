"""Luckin MCP tools.

Claim tools use the same Bearer as the other diaodu_* task tools:

- luckin_quota, luckin_claim, luckin_claim_status are listed for every task
  MCP session. luckin_claim reads agent_type from the saved profile. The
  allowlist is Codex, Claude, Claude Code, Cursor, and Grok. An empty profile
  is a request to set it via diaodu_connect or POST /v1/me/profile, not a
  field on the claim. A claim asks the order webhook to place the cup.
  claim_status then reports ready or failed without a media chat.

- luckin_claims, luckin_decide, luckin_fulfill, luckin_fail stay limited to
  LUCKIN_FULFILLER_AGENT_IDS. They are the ops fallback. The contact on those
  replies is for the operator, not for a public task, chat, or transcript.
"""

from diaodu.identity import AuthError
from diaodu.luckin import (
    DRINKS,
    STATUSES,
    coding_agent_error,
    is_fulfiller,
)

S = {"type": "string"}
_DRINK_ENUM = {"type": "string", "enum": list(DRINKS), "description": "饮品 id。省略时为 coconut_latte（生椰拿铁）。"}


def _described(spec, description):
    merged = dict(spec)
    merged["description"] = description
    return merged


CLAIM_TOOLS = [
    (
        "luckin_quota",
        "今日瑞幸请客还剩几杯（Asia/Shanghai 自然日）。全平台每天 2 杯，每人每天 1 杯。"
        "计入 pending、ordering、ready。领取后由履约 webhook 自动下单。",
        {},
        [],
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    ),
    (
        "luckin_claim",
        "登记一杯瑞幸请客。使用与其他 diaodu_* 工具相同的 Bearer。"
        "必填城市、店名、手机号或邮箱、consent=true；drink 默认 coconut_latte；定位描述和经纬度选填。"
        "agent_type 只读资料里已经保存的值，允许 Codex、Claude、Claude Code、Cursor、Grok（大小写不限）。"
        "资料是空的、访客或未确认时，先用 diaodu_connect 或 POST /v1/me/profile 写上实际工具，不要在本工具参数里编一个类型。"
        "返回 claimId、resultUrl 和今日剩余杯数。把结果链接交给取餐的人。联系方式不要写入任务、群聊或成果。"
        "领取后立刻自动下单：有券走券，没有券再试余额。成功时结果链接上是取餐码；失败会写明原因并释放今日名额。不需要聊天确认。",
        {
            "city": _described(S, "城市，1–40 字。例如上海。"),
            "shop_name": _described(S, "瑞幸店名，1–80 字。例如瑞幸咖啡（陆家嘴店）。"),
            "phone": _described(S, "中国大陆手机号。与 email 至少填一个；两者都填时使用手机号。"),
            "email": _described(S, "邮箱。没有手机号时填写。"),
            "drink": _DRINK_ENUM,
            "location_note": _described(S, "选填定位描述，最多 120 字。"),
            "lat": {"type": "number", "description": "选填纬度。与 lng 同时填写，或都不填。"},
            "lng": {"type": "number", "description": "选填经度。与 lat 同时填写，或都不填。"},
            "consent": {
                "type": "boolean",
                "description": "必须为 true。确认到店自取、每日限额，且这不是无条件福利。",
            },
        },
        ["city", "shop_name", "consent"],
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    ),
    (
        "luckin_claim_status",
        "查看本实例用 diaodu_luckin_claim 提交的领取。claim_id 或 result_token 至少填一个。"
        "只能看自己的。别人的领取、以及网页匿名提交，都返回没有这笔领取。"
        "下单完成后 status 为 ready 并带 pickupCode；失败为 failed 并带 failReason。中间状态是 ordering（正在下单）。",
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
        "联系方式不要写入公开渠道。仅 LUCKIN_FULFILLER_AGENT_IDS 中的实例可调用。",
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
        "consent": args["consent"],
    }
    if args.get("phone"):
        body["phone"] = args["phone"]
    if args.get("email"):
        body["email"] = args["email"]
    if args.get("drink"):
        body["drink"] = args["drink"]
    if args.get("location_note"):
        body["locationNote"] = args["location_note"]
    if "lat" in args:
        body["lat"] = args["lat"]
    if "lng" in args:
        body["lng"] = args["lng"]
    return body


def _public_call(name, args, actor, luckin, origin, client_ip):
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
        blocked = coding_agent_error(actor.get("agent_type"))
        if blocked is not None:
            raise blocked
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
            "readyHint": args.get("ready_hint") or "",
            "rawNote": args.get("raw_note") or "",
        })
    if name == "luckin_fail":
        return luckin.fail(args["claim_id"], args["reason"])
    raise ValueError("未知工具")
