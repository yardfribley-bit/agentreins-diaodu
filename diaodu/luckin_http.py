"""Public claim routes and the allowlisted fulfiller API.

Anonymous visitors claim a cup and read the result with the secret token.
The claim itself asks the order webhook to place the cup. Listing and manual
state changes require a Bearer token whose agent id is in
LUCKIN_FULFILLER_AGENT_IDS. Those calls are the ops fallback.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs

from diaodu.identity import AuthError
from diaodu.luckin import LuckinError, is_fulfiller


def _ip(handler):
    return handler.headers.get("X-Real-IP") or handler.client_address[0]


def _origin(handler, luckin):
    if luckin.public_origin:
        return luckin.public_origin
    host = handler.headers.get("Host", "")
    if not host or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,200}", host):
        host = "127.0.0.1"
    return "http://" + host


def _require_fulfiller(handler, identity):
    authorization = handler.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise AuthError("需要 Bearer 履约凭证", 401)
    token = authorization[7:].strip()
    if not token or identity is None:
        raise AuthError("需要 Bearer 履约凭证", 401)
    actor = identity.agent(token)
    if not is_fulfiller(actor):
        raise AuthError("此实例不在 LUCKIN_FULFILLER_AGENT_IDS 允许名单", 403)
    return actor


def _guard(handler, action):
    try:
        action()
    except LuckinError as exc:
        payload = {"error": str(exc)}
        if exc.code:
            payload["code"] = exc.code
        handler._json(exc.status, payload)
    except AuthError as exc:
        handler._fail(exc)


def get(handler, library, identity, parsed):
    luckin = library.luckin
    path = parsed.path

    def run():
        if path in ("/v1/luckin/quota",):
            luckin.limit("read", _ip(handler), luckin.read_ip_limit)
            handler._json(200, luckin.quota())
            return
        if path == "/v1/luckin/result" or path.startswith("/v1/luckin/result/"):
            luckin.limit("read", _ip(handler), luckin.read_ip_limit)
            token = path[len("/v1/luckin/result/"):] if path.startswith("/v1/luckin/result/") else ""
            if not token or "/" in token:
                raise LuckinError("没有这笔领取", 404, code="not_found")
            handler._json(200, luckin.public_by_token(token))
            return
        if path == "/v1/luckin/claims":
            _require_fulfiller(handler, identity)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if set(query) - {"status"} or any(len(values) != 1 for values in query.values()):
                raise LuckinError("查询参数不合法")
            status = query.get("status", ["ordering"])[0]
            handler._json(200, {"claims": luckin.list_claims(status)})
            return
        handler._json(404, {"error": "没有这个地址"})

    _guard(handler, run)


def post(handler, library, identity, parts):
    luckin = library.luckin

    def run():
        if parts == ["v1", "luckin", "claims"]:
            luckin.limit("claim-ip", _ip(handler), luckin.claim_ip_limit)
            body = handler._body(limit=32_768)
            handler._json(201, luckin.create(body, _origin(handler, luckin)))
            return
        if len(parts) == 5 and parts[:3] == ["v1", "luckin", "claims"] and parts[4] in ("decide", "fulfill", "fail"):
            _require_fulfiller(handler, identity)
            body = handler._body(limit=32_768)
            claim_id = parts[3]
            if parts[4] == "decide":
                if set(body) - {"action", "note"}:
                    raise LuckinError("不支持这些字段：" + "、".join(sorted(set(body) - {"action", "note"})))
                result = luckin.decide(claim_id, body.get("action"), body.get("note") or "")
            elif parts[4] == "fulfill":
                result = luckin.fulfill(claim_id, body)
            else:
                if set(body) - {"reason"}:
                    raise LuckinError("不支持这些字段：" + "、".join(sorted(set(body) - {"reason"})))
                result = luckin.fail(claim_id, body.get("reason"))
            handler._json(200, result)
            return
        handler._json(404, {"error": "没有这个地址"})

    _guard(handler, run)
