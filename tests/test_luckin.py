"""Luckin promo queue: quota, masks, transitions, and fulfiller auth."""

import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from diaodu.discovery import llms_text
from diaodu.identity import Identity
from diaodu.library import Library
from diaodu.luckin import (
    Luckin,
    LuckinError,
    coding_agent_status,
    mask_email,
    mask_phone,
    normalize_email,
    normalize_phone,
)
from diaodu.server import make_handler

ROOT = Path(__file__).resolve().parents[1]
PHONE = "13800000001"
PHONE_B = "13900000002"
PHONE_C = "13700000003"
PHONE_D = "13600000004"


def claim_body(phone=PHONE, **extra):
    body = {
        "phone": phone,
        "city": "上海",
        "shopName": "瑞幸咖啡（陆家嘴店）",
        "drink": "coconut_latte",
        "consent": True,
    }
    body.update(extra)
    return body


class MaskTests(unittest.TestCase):
    def test_phone_and_email_masks(self):
        self.assertEqual(normalize_phone(" +86 138-0000-0001 "), PHONE)
        self.assertEqual(normalize_phone("008613800000001"), PHONE)
        self.assertEqual(normalize_phone("8613800000001"), PHONE)
        self.assertEqual(mask_phone(PHONE), "138****0001")
        self.assertEqual(normalize_email(" Hello.World@Example.com "), "hello.world@example.com")
        self.assertEqual(mask_email("hello.world@example.com"), "h***d@example.com")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hooks = []
        self.store = Luckin(Path(self.tmp.name), "/task", "https://www.chuhaijian.com", poster=self._poster)
        self._saved = {
            "LUCKIN_PENDING_WEBHOOK_URL": os.environ.get("LUCKIN_PENDING_WEBHOOK_URL"),
            "LUCKIN_FULFILLER_AGENT_IDS": os.environ.get("LUCKIN_FULFILLER_AGENT_IDS"),
        }
        os.environ["LUCKIN_PENDING_WEBHOOK_URL"] = "https://media.example/luckin"

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _poster(self, url, payload):
        self.hooks.append((url, payload))

    def test_quota_mask_and_states(self):
        early = datetime(2026, 10, 29, 15, 30, tzinfo=timezone.utc).timestamp()
        late = datetime(2026, 10, 29, 16, 30, tzinfo=timezone.utc).timestamp()
        self.store.clock = lambda: early
        first = self.store.create(claim_body(locationNote="近2号口 " + PHONE, lat=31.23, lng=121.47), "https://www.chuhaijian.com")
        self.assertEqual(first["contactMasked"], "138****0001")
        self.assertNotIn(PHONE, json.dumps(first, ensure_ascii=False))
        self.assertNotIn("productId", first)
        self.assertNotIn("SP2077-01134", json.dumps(first))
        self.assertEqual(len(self.hooks), 1)
        hook = self.hooks[0][1]
        self.assertTrue(set(hook) <= {
            "claimId", "contactMasked", "city", "shopName", "locationNote",
            "drink", "tasteNote", "createdAt", "resultUrl",
        })
        self.assertEqual(hook["drink"], "生椰拿铁")
        self.assertNotIn(PHONE, json.dumps(hook, ensure_ascii=False))
        self.assertIn("138****0001", hook["locationNote"])
        self.assertIn("31.230000,121.470000", hook["locationNote"])
        self.assertTrue(hook["createdAt"].endswith("+08:00"))
        self.assertIn("/task/luckin/r/", hook["resultUrl"])
        self.assertNotIn(hook["resultUrl"].rsplit("/", 1)[-1], json.dumps(self.store.list_claims("pending")))

        second = self.store.create(claim_body(PHONE_B, drink="latte"), "https://www.chuhaijian.com")
        with self.assertRaises(Exception) as full:
            self.store.create(claim_body(PHONE_C), "https://www.chuhaijian.com")
        self.assertEqual(full.exception.status, 409)
        self.assertEqual(full.exception.code, "quota_full")
        self.assertEqual(self.store.quota()["remaining"], 0)
        self.assertEqual(self.store.quota()["day"], "2026-10-29")

        listed = self.store.list_claims("pending")
        match = next(item for item in listed if item["claimId"] == first["claimId"])
        self.assertEqual(match["contact"], PHONE)
        self.assertEqual(match["productId"], "1262")
        self.assertEqual(match["sku"], "SP2077-01134")
        self.assertAlmostEqual(match["lat"], 31.23)

        with self.assertRaises(Exception) as early_fulfill:
            self.store.fulfill(first["claimId"], {"orderId": "1", "pickupCode": "9"})
        self.assertEqual(early_fulfill.exception.code, "invalid_state")
        ordering = self.store.decide(first["claimId"], "approve", "对话已确认")
        self.assertEqual(ordering["status"], "ordering")
        ready = self.store.fulfill(first["claimId"], {
            "orderId": "LK1",
            "pickupCode": "3891",
            "shopName": "瑞幸咖啡（陆家嘴店）",
            "readyHint": "到店报码",
            "rawNote": "secret " + PHONE,
        })
        self.assertEqual(ready["status"], "ready")
        again = self.store.fulfill(first["claimId"], {"orderId": "LK1", "pickupCode": "3891"})
        self.assertEqual(again["pickupCode"], "3891")
        with self.assertRaises(Exception) as other_code:
            self.store.fulfill(first["claimId"], {"orderId": "LK2", "pickupCode": "0000"})
        self.assertEqual(other_code.exception.status, 409)

        public = self.store.public_by_token(first["resultToken"])
        self.assertEqual(public["pickupCode"], "3891")
        self.assertEqual(public["shopName"], "瑞幸咖啡（陆家嘴店）")
        self.assertNotIn(PHONE, json.dumps(public, ensure_ascii=False))
        self.assertNotIn("secret", json.dumps(public, ensure_ascii=False))
        self.assertNotIn("resultToken", public)
        with self.assertRaises(Exception) as missing:
            self.store.public_by_token("")
        self.assertEqual(missing.exception.status, 404)
        with self.assertRaises(Exception) as unknown:
            self.store.public_by_token("a" * 24)
        self.assertEqual(unknown.exception.status, 404)

        failed = self.store.fail(second["claimId"], "门店暂不可下")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.store.quota()["remaining"], 1)
        self.store.clock = lambda: late
        self.assertEqual(self.store.quota()["day"], "2026-10-30")
        self.assertEqual(self.store.quota()["remaining"], 2)
        retried = self.store.decide(second["claimId"], "approve")
        self.assertEqual(retried["claimId"], second["claimId"])
        self.assertEqual(retried["status"], "ordering")
        self.assertEqual(retried["failReason"], "")
        self.assertEqual(self.store.quota()["remaining"], 2)
        self.assertEqual(len(self.hooks), 2)

    def test_failed_retry_does_not_open_a_second_cup(self):
        created = self.store.create(claim_body(), "https://www.chuhaijian.com")
        self.store.fail(created["claimId"], "失败")
        again = self.store.decide(created["claimId"], "approve")
        self.assertEqual(again["claimId"], created["claimId"])
        with self.store._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        self.assertEqual(count, 1)
        self.store.fail(created["claimId"], "再次失败")
        other = self.store.create(claim_body(PHONE_B), "https://www.chuhaijian.com")
        self.store.create(claim_body(PHONE_C), "https://www.chuhaijian.com")
        with self.assertRaises(Exception) as blocked:
            self.store.decide(created["claimId"], "approve")
        self.assertEqual(blocked.exception.code, "quota_full")
        self.store.fail(other["claimId"], "让出名额")
        pulled = self.store.decide(created["claimId"], "approve")
        self.assertEqual(pulled["status"], "ordering")
        with self.store._connect() as db:
            active = db.execute(
                "SELECT COUNT(*) FROM claims WHERE status IN ('pending','ordering','ready')"
            ).fetchone()[0]
        self.assertEqual(active, 2)

    def test_reject_and_cancel_free_the_person_slot(self):
        first = self.store.create(claim_body(), "https://www.chuhaijian.com")
        self.store.decide(first["claimId"], "reject", "不送")
        with self.assertRaises(Exception) as stuck:
            self.store.decide(first["claimId"], "approve")
        self.assertEqual(stuck.exception.code, "invalid_state")
        second = self.store.create(claim_body(), "https://www.chuhaijian.com")
        self.assertNotEqual(second["claimId"], first["claimId"])
        with self.store._connect() as db:
            db.execute("UPDATE claims SET status='cancelled' WHERE id=?", (second["claimId"],))
        third = self.store.create(claim_body(email="a@example.com", phone=""), "https://www.chuhaijian.com")
        self.assertEqual(third["contactMasked"], "*@example.com")
        duplicate = claim_body(phone="", email="A@Example.com")
        with self.assertRaises(Exception) as taken:
            self.store.create(duplicate, "https://www.chuhaijian.com")
        self.assertEqual(taken.exception.code, "already_claimed")

    def test_same_phone_same_hash_across_shanghai_days(self):
        early = datetime(2026, 10, 29, 15, 30, tzinfo=timezone.utc).timestamp()
        late = datetime(2026, 10, 29, 16, 30, tzinfo=timezone.utc).timestamp()
        self.store.clock = lambda: early
        self.store.create(claim_body(), "https://www.chuhaijian.com")
        self.store.clock = lambda: late
        self.store.create(claim_body(), "https://www.chuhaijian.com")
        with self.store._connect() as db:
            rows = db.execute("SELECT person_hash, day FROM claims ORDER BY created").fetchall()
        self.assertEqual(rows[0]["person_hash"], rows[1]["person_hash"])
        self.assertEqual([row["day"] for row in rows], ["2026-10-29", "2026-10-30"])

    def test_webhook_failure_keeps_the_claim_and_hides_the_phone(self):
        os.environ["LUCKIN_PENDING_WEBHOOK_URL"] = "http://127.0.0.1:1/luckin"
        self.store.poster = None
        err = io.StringIO()
        with redirect_stderr(err):
            created = self.store.create(claim_body(), "https://www.chuhaijian.com")
        self.assertEqual(created["status"], "pending")
        self.assertNotIn(PHONE, err.getvalue())
        self.assertIn(created["claimId"], err.getvalue())
        os.environ["LUCKIN_PENDING_WEBHOOK_URL"] = "file:///tmp/luckin-secret"
        err = io.StringIO()
        with redirect_stderr(err):
            self.store.fail(created["claimId"], "腾出名额")
            self.store.create(claim_body(PHONE_B), "https://www.chuhaijian.com")
        self.assertNotIn(PHONE_B, err.getvalue())
        self.assertIn("unsupported scheme", err.getvalue())

    def test_coding_agent_allowlist(self):
        allowed = (
            "Codex", "CODEX", "OpenAI Codex", "codex-cli",
            "Claude", "Anthropic Claude",
            "Claude Code", "claude-code", "ClaudeCode",
            "Cursor", "Cursor Agent", "cursor-agent", "CursorAgent",
            "Grok", "gRoK", "xAI Grok", "Grok 4", "grok-4",
        )
        for name in allowed:
            self.assertEqual(coding_agent_status(name), "ok", name)
        for name in ("", "   ", None, "访客", "人员", "未确认", "工具未确认"):
            self.assertEqual(coding_agent_status(name), "unset", name)
        for name in ("WorkBuddy", "豆包", "ChatGPT", "Gemini", "codexx"):
            self.assertEqual(coding_agent_status(name), "denied", name)

    def test_mcp_owner_cannot_read_someone_elses_claim(self):
        owner = "ag_" + "ab" * 12
        other = "ag_" + "cd" * 12
        created = self.store.create(claim_body(), "https://www.chuhaijian.com", agent_id=owner)
        own = self.store.claim_for_agent(owner, claim_id=created["claimId"])
        self.assertEqual(own["claimId"], created["claimId"])
        self.assertEqual(own["contactMasked"], "138****0001")
        self.assertNotIn("contact", own)
        token = created["resultToken"]
        by_token = self.store.claim_for_agent(owner, result_token=token, origin="https://www.chuhaijian.com")
        self.assertEqual(by_token["resultUrl"], created["resultUrl"])
        for kwargs in (
            {"claim_id": created["claimId"]},
            {"result_token": token},
        ):
            with self.assertRaises(LuckinError) as raised:
                self.store.claim_for_agent(other, **kwargs)
            self.assertEqual(raised.exception.status, 404)
            self.assertEqual(raised.exception.code, "not_found")
        web = self.store.create(claim_body(PHONE_B), "https://www.chuhaijian.com")
        with self.assertRaises(LuckinError) as raised:
            self.store.claim_for_agent(owner, claim_id=web["claimId"])
        self.assertEqual(raised.exception.code, "not_found")
        with self.assertRaises(LuckinError) as raised:
            self.store.claim_for_agent(owner, claim_id=created["claimId"], result_token="a" * 24)
        self.assertEqual(raised.exception.code, "not_found")

    def test_legacy_database_gains_agent_id(self):
        root = Path(self.tmp.name) / "legacy"
        root.mkdir()
        db = sqlite3.connect(root / "luckin.sqlite3")
        db.execute(
            """CREATE TABLE claims (
              id TEXT PRIMARY KEY,
              person_hash TEXT NOT NULL,
              contact_kind TEXT NOT NULL,
              contact_masked TEXT NOT NULL,
              contact_sealed BLOB NOT NULL,
              city TEXT NOT NULL,
              shop_name TEXT NOT NULL,
              location_note TEXT NOT NULL DEFAULT '',
              lat REAL,
              lng REAL,
              drink_id TEXT NOT NULL,
              drink_name TEXT NOT NULL,
              product_id TEXT NOT NULL DEFAULT '',
              sku TEXT NOT NULL DEFAULT '',
              drink_spec TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL,
              day TEXT NOT NULL,
              staff_note TEXT NOT NULL DEFAULT '',
              fail_reason TEXT NOT NULL DEFAULT '',
              order_id TEXT NOT NULL DEFAULT '',
              pickup_code TEXT NOT NULL DEFAULT '',
              fulfill_shop TEXT NOT NULL DEFAULT '',
              ready_hint TEXT NOT NULL DEFAULT '',
              raw_note TEXT NOT NULL DEFAULT '',
              created REAL NOT NULL,
              updated REAL NOT NULL,
              ready_at REAL,
              result_token_hash TEXT NOT NULL UNIQUE
            )"""
        )
        db.commit()
        db.close()
        store = Luckin(root, "/task", "https://www.chuhaijian.com")
        created = store.create(claim_body(), "https://www.chuhaijian.com", agent_id="ag_" + "ef" * 12)
        self.assertTrue(created["claimId"].startswith("lk_"))
        found = store.claim_for_agent("ag_" + "ef" * 12, claim_id=created["claimId"])
        self.assertEqual(found["status"], "pending")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.library = Library(root)
        self.identity = Identity(root / "identity.sqlite3")
        self.media = self.identity.connect("媒体助手", "Grok")
        self.other = self.identity.connect("路人", "Codex")
        self._saved = {
            "LUCKIN_FULFILLER_AGENT_IDS": os.environ.get("LUCKIN_FULFILLER_AGENT_IDS"),
            "LUCKIN_PENDING_WEBHOOK_URL": os.environ.get("LUCKIN_PENDING_WEBHOOK_URL"),
        }
        os.environ["LUCKIN_FULFILLER_AGENT_IDS"] = self.media["agent_id"]
        os.environ.pop("LUCKIN_PENDING_WEBHOOK_URL", None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, self.library, self.identity))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def req(self, method, path, body=None, token=None, headers=None):
        extra = {} if headers is None else dict(headers)
        if token:
            extra["Authorization"] = "Bearer " + token
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            extra.setdefault("Content-Type", "application/json")
        request = Request(self.base + path, data=data, headers=extra, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                if path.endswith("/") or path.startswith("/luckin") or path.endswith(".txt") or "text/" in content_type or "javascript" in content_type:
                    if "json" not in content_type:
                        return response.status, raw.decode()
                return response.status, json.loads(raw.decode() or "null")
        except HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode() or "null")
            except json.JSONDecodeError:
                payload = raw.decode()
            return exc.code, payload

    def test_page_discovery_and_public_flow(self):
        status, page = self.req("GET", "/luckin/")
        self.assertEqual(status, 200)
        self.assertIn("AgentReins 老板请客体验 · 非无条件福利 · 须到店自取 · 每日两杯送完即止 · 每人每天限一杯", page)
        self.assertIn("生椰拿铁", self.req("GET", "/luckin/luckin.js")[1])
        self.assertNotIn("SP2077-01134", page)
        self.assertNotIn("1262", page)
        self.assertNotIn("__", page)
        self.assertEqual(self._status("/luckin"), 308)
        turned = self.req("GET", "/luckin/r/short")
        self.assertEqual(turned[0], 404)
        guide = llms_text()
        self.assertIn("https://www.chuhaijian.com/task/luckin/", guide)
        self.assertNotIn("LUCKIN_", guide)
        self.assertIn("每天请人喝两杯瑞幸", (ROOT / "static" / "app.js").read_text(encoding="utf-8"))
        doc = (ROOT / "docs" / "luckin-promo.md").read_text(encoding="utf-8")
        for snippet in ("LUCKIN_FULFILLER_AGENT_IDS", "LUCKIN_PENDING_WEBHOOK_URL", "通过/下单", "coconut_latte", "SP2077-01134"):
            self.assertIn(snippet, doc)
        self.assertIn("diaodu_luckin_claim", guide)
        self.assertIn("diaodu_luckin_quota", guide)
        self.assertIn("diaodu_luckin_claim_status", guide)
        guide_md = (ROOT / "diaodu" / "agent-guide.md").read_text(encoding="utf-8")
        for snippet in (
            "diaodu_luckin_claim",
            "diaodu_luckin_quota",
            "diaodu_luckin_claim_status",
            "POST /v1/me/profile",
            "Claude Code",
        ):
            self.assertIn(snippet, guide_md)
            self.assertIn(snippet, doc)

        missing, missing_body = self.req("GET", "/v1/luckin/result")
        self.assertEqual(missing, 404)
        self.assertEqual(missing_body["code"], "not_found")
        self.assertEqual(self.req("GET", "/v1/luckin/result/")[0], 404)
        self.assertEqual(self.req("GET", "/v1/luckin/result/" + "a" * 24)[0], 404)

        status, created = self.req("POST", "/v1/luckin/claims", claim_body())
        self.assertEqual(status, 201, created)
        duplicate = self.req("POST", "/v1/luckin/claims", claim_body())
        self.assertEqual(duplicate[0], 409)
        self.assertEqual(duplicate[1]["code"], "already_claimed")
        blob = json.dumps(created, ensure_ascii=False)
        self.assertNotIn(PHONE, blob)
        self.assertNotIn("1262", blob)
        self.assertIn("/luckin/r/", created["resultUrl"])
        token = created["resultToken"]
        status, result = self.req("GET", "/v1/luckin/result/" + token)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "pending")
        self.assertNotIn(PHONE, json.dumps(result, ensure_ascii=False))
        self.assertNotIn("pickupCode", result)

        self.assertEqual(self.req("GET", "/v1/luckin/claims?status=pending")[0], 401)
        self.assertEqual(self.req("GET", "/v1/luckin/claims?status=pending", token="nope")[0], 401)
        self.assertEqual(self.req("POST", "/v1/luckin/claims/" + created["claimId"] + "/decide", {"action": "approve"}, token=self.other["token"])[0], 403)
        cookie = self.req("GET", "/v1/luckin/claims?status=pending", headers={"Cookie": "diaodu_session=" + self.media["token"]})
        self.assertEqual(cookie[0], 401)

        status, listed = self.req("GET", "/v1/luckin/claims?status=pending", token=self.media["token"])
        self.assertEqual(status, 200)
        self.assertEqual(listed["claims"][0]["contact"], PHONE)
        self.assertEqual(listed["claims"][0]["productId"], "1262")
        self.assertNotIn(token, json.dumps(listed))

        self.assertEqual(self.req("POST", "/v1/luckin/claims/" + created["claimId"] + "/fulfill", {"orderId": "1", "pickupCode": "2"}, token=self.media["token"])[0], 409)
        approved = self.req("POST", "/v1/luckin/claims/" + created["claimId"] + "/decide", {"action": "approve", "note": "通过/下单"}, token=self.media["token"])
        self.assertEqual(approved[0], 200)
        self.assertEqual(approved[1]["status"], "ordering")
        fulfilled = self.req("POST", "/v1/luckin/claims/" + created["claimId"] + "/fulfill", {
            "orderId": "LK9",
            "pickupCode": "2468",
            "readyHint": "到店自取",
            "rawNote": "hidden " + PHONE,
        }, token=self.media["token"])
        self.assertEqual(fulfilled[0], 200)
        status, ready = self.req("GET", "/v1/luckin/result/" + token)
        self.assertEqual(ready["pickupCode"], "2468")
        self.assertIn("orderTime", ready)
        self.assertNotIn(PHONE, json.dumps(ready, ensure_ascii=False))
        self.assertNotIn("hidden", json.dumps(ready, ensure_ascii=False))

        status, second = self.req("POST", "/v1/luckin/claims", claim_body(PHONE_B))
        self.assertEqual(status, 201, second)
        failed = self.req("POST", "/v1/luckin/claims/" + second["claimId"] + "/fail", {"reason": "没下成"}, token=self.media["token"])
        self.assertEqual(failed[0], 200)
        self.assertEqual(failed[1]["status"], "failed")
        status, third = self.req("POST", "/v1/luckin/claims", claim_body(PHONE_C))
        self.assertEqual(status, 201, third)
        blocked = self.req("POST", "/v1/luckin/claims", claim_body(PHONE_D))
        self.assertEqual(blocked[0], 409)
        self.assertEqual(blocked[1]["code"], "quota_full")

    def test_ip_rate_limit(self):
        self.library.luckin.claim_ip_limit = 1
        self.assertEqual(self.req("POST", "/v1/luckin/claims", claim_body())[0], 201)
        limited = self.req("POST", "/v1/luckin/claims", claim_body(PHONE_B))
        self.assertEqual(limited[0], 429)
        self.assertEqual(limited[1]["code"], "rate_limited")

    def _status(self, path):
        class _Stop(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise HTTPError(req.full_url, code, msg, headers, fp)

        opener = urllib.request.build_opener(_Stop)
        try:
            with opener.open(self.base + path, timeout=5) as response:
                return response.status
        except HTTPError as exc:
            return exc.code

    def test_mcp_allowlist(self):
        status, created = self.req("POST", "/v1/luckin/claims", claim_body(phone="", email="guest@example.com"))
        self.assertEqual(status, 201, created)
        hidden = self.req("POST", "/v1/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=self.other["token"])
        names = {tool["name"] for tool in hidden[1]["result"]["tools"]}
        self.assertNotIn("diaodu_luckin_claims", names)
        shown = self.req("POST", "/v1/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=self.media["token"])
        names = {tool["name"] for tool in shown[1]["result"]["tools"]}
        self.assertIn("diaodu_luckin_claims", names)
        denied = self.req("POST", "/v1/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "diaodu_luckin_claims", "arguments": {"status": "pending"}},
        }, token=self.other["token"])
        self.assertTrue(denied[1]["result"]["isError"])
        self.assertIn("403", denied[1]["result"]["content"][0]["text"])
        allowed = self.req("POST", "/v1/mcp", {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "diaodu_luckin_decide", "arguments": {"claim_id": created["claimId"], "action": "reject"}},
        }, token=self.media["token"])
        self.assertFalse(allowed[1]["result"]["isError"], allowed[1])
        payload = json.loads(allowed[1]["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["contact"], "guest@example.com")

    def _mcp(self, token, method, params=None, req_id=1):
        return self.req("POST", "/v1/mcp", {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }, token=token)

    def _tool_names(self, token):
        status, body = self._mcp(token, "tools/list")
        self.assertEqual(status, 200, body)
        return {tool["name"]: tool for tool in body["result"]["tools"]}

    def _call(self, token, name, arguments=None):
        status, body = self._mcp(token, "tools/call", {"name": name, "arguments": arguments or {}})
        self.assertEqual(status, 200, body)
        result = body["result"]
        text = result["content"][0]["text"]
        payload = json.loads(text)
        return result.get("isError"), payload

    def test_mcp_claim_quota_and_ownership(self):
        guest = self._tool_names(self.other["token"])
        for name in ("diaodu_luckin_quota", "diaodu_luckin_claim", "diaodu_luckin_claim_status"):
            self.assertIn(name, guest)
        for name in ("diaodu_luckin_claims", "diaodu_luckin_decide", "diaodu_luckin_fulfill", "diaodu_luckin_fail"):
            self.assertNotIn(name, guest)
        claim_tool = guest["diaodu_luckin_claim"]
        schema = claim_tool["inputSchema"]
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(schema["required"], ["city", "shop_name", "consent"])
        self.assertIn("coconut_latte", schema["properties"]["drink"]["enum"])
        self.assertNotIn("agent_type", schema["properties"])
        self.assertTrue(guest["diaodu_luckin_quota"]["annotations"]["readOnlyHint"])
        self.assertTrue(guest["diaodu_luckin_claim_status"]["annotations"]["readOnlyHint"])
        media = self._tool_names(self.media["token"])
        self.assertIn("diaodu_luckin_fulfill", media)
        self.assertIn("diaodu_luckin_claim", media)

        error, quota = self._call(self.other["token"], "diaodu_luckin_quota")
        self.assertFalse(error, quota)
        self.assertEqual(quota["remaining"], 2)
        self.assertEqual(quota["timezone"], "Asia/Shanghai")
        self.assertEqual(quota["limit"], 2)

        error, anon = self._call(None, "diaodu_luckin_claim", {
            "city": "上海", "shop_name": "瑞幸咖啡（陆家嘴店）", "phone": PHONE, "consent": True,
        })
        self.assertTrue(error)
        self.assertEqual(anon["code"], 401)

        error, invented = self._call(self.other["token"], "diaodu_luckin_claim", {
            "city": "上海",
            "shop_name": "瑞幸咖啡（陆家嘴店）",
            "phone": PHONE,
            "consent": True,
            "agent_type": "Codex",
        })
        self.assertTrue(error)
        self.assertIn("agent_type", invented["message"])

        error, created = self._call(self.other["token"], "diaodu_luckin_claim", {
            "city": "上海",
            "shop_name": "瑞幸咖啡（陆家嘴店）",
            "phone": PHONE,
            "location_note": "近2号口",
            "consent": True,
        })
        self.assertFalse(error, created)
        self.assertTrue(created["claimId"].startswith("lk_"))
        self.assertIn("/luckin/r/", created["resultUrl"])
        self.assertEqual(created["remaining"], 1)
        self.assertEqual(created["quota"]["remaining"], 1)
        self.assertEqual(created["drink"], "生椰拿铁")
        self.assertEqual(created["status"], "pending")
        blob = json.dumps(created, ensure_ascii=False)
        self.assertNotIn(PHONE, blob)
        self.assertIn("138****0001", blob)

        error, again = self._call(self.other["token"], "diaodu_luckin_claim", {
            "city": "上海", "shop_name": "瑞幸咖啡（陆家嘴店）", "phone": PHONE, "consent": True,
        })
        self.assertTrue(error)
        self.assertEqual(again["reason"], "already_claimed")
        self.assertEqual(again["code"], 409)
        error, quota = self._call(self.other["token"], "diaodu_luckin_quota")
        self.assertEqual(quota["remaining"], 1)

        error, by_id = self._call(self.other["token"], "diaodu_luckin_claim_status", {"claim_id": created["claimId"]})
        self.assertFalse(error, by_id)
        self.assertEqual(by_id["claimId"], created["claimId"])
        self.assertEqual(by_id["status"], "pending")
        self.assertNotIn(PHONE, json.dumps(by_id, ensure_ascii=False))
        error, by_token = self._call(self.other["token"], "diaodu_luckin_claim_status", {"result_token": created["resultToken"]})
        self.assertFalse(error, by_token)
        self.assertEqual(by_token["claimId"], created["claimId"])
        self.assertEqual(by_token["resultUrl"], created["resultUrl"])

        error, hidden = self._call(self.media["token"], "diaodu_luckin_claim_status", {"claim_id": created["claimId"]})
        self.assertTrue(error)
        self.assertEqual(hidden["reason"], "not_found")
        error, hidden_token = self._call(self.media["token"], "diaodu_luckin_claim_status", {"result_token": created["resultToken"]})
        self.assertTrue(error)
        self.assertEqual(hidden_token["reason"], "not_found")

        error, listed = self._call(self.media["token"], "diaodu_luckin_claims", {"status": "pending"})
        self.assertFalse(error, listed)
        self.assertEqual(listed["claims"][0]["contact"], PHONE)
        error, denied = self._call(self.other["token"], "diaodu_luckin_fulfill", {
            "claim_id": created["claimId"], "order_id": "1", "pickup_code": "2",
        })
        self.assertTrue(denied)
        self.assertEqual(denied["code"], 403)

        status, web = self.req("POST", "/v1/luckin/claims", claim_body(PHONE_B))
        self.assertEqual(status, 201, web)
        error, web_status = self._call(self.other["token"], "diaodu_luckin_claim_status", {"claim_id": web["claimId"]})
        self.assertTrue(error)
        self.assertEqual(web_status["reason"], "not_found")
        error, full = self._call(self.other["token"], "diaodu_luckin_claim", {
            "city": "上海", "shop_name": "瑞幸咖啡（陆家嘴店）", "phone": PHONE_C, "consent": True,
        })
        self.assertTrue(error)
        self.assertEqual(full["reason"], "quota_full")
        self.assertEqual(self.req("GET", "/luckin/")[0], 200)
        self.assertEqual(self.req("GET", "/v1/luckin/result/" + created["resultToken"])[1]["claimId"], created["claimId"])

    def test_mcp_agent_type_gate(self):
        buddy = self.identity.connect("办公助手", "WorkBuddy")
        doubao = self.identity.connect("豆包助手", "豆包")
        blank = self.identity.connect("未填工具", "Codex")
        claude = self.identity.connect("克劳德", "Claude Code")
        grok = self.identity.connect("格罗克", "gRoK")
        with self.identity.db() as db:
            db.execute("UPDATE agents SET type='' WHERE id=?", (blank["agent_id"],))

        error, quota = self._call(buddy["token"], "diaodu_luckin_quota")
        self.assertFalse(error, quota)
        self.assertEqual(quota["remaining"], 2)

        error, blocked = self._call(buddy["token"], "diaodu_luckin_claim", {
            "city": "上海", "shop_name": "瑞幸咖啡（陆家嘴店）", "phone": PHONE, "consent": True,
        })
        self.assertTrue(error)
        self.assertEqual(blocked["code"], 403)
        self.assertEqual(blocked["reason"], "agent_type_not_allowed")
        self.assertIn("/v1/me/profile", blocked["message"])
        self.assertIn("Codex", blocked["message"])

        error, empty = self._call(blank["token"], "diaodu_luckin_claim", {
            "city": "上海", "shop_name": "瑞幸咖啡（陆家嘴店）", "email": "guest@example.com", "consent": True,
        })
        self.assertTrue(error)
        self.assertEqual(empty["reason"], "agent_type_required")
        self.assertIn("diaodu_connect", empty["message"])
        self.assertIn("/v1/me/profile", empty["message"])
        self.assertIn("不要在 diaodu_luckin_claim", empty["message"])

        error, folded = self._call(doubao["token"], "diaodu_luckin_claim", {
            "city": "上海", "shop_name": "瑞幸咖啡（陆家嘴店）", "phone": PHONE_B, "consent": True,
        })
        self.assertTrue(error)
        self.assertEqual(folded["reason"], "agent_type_not_allowed")

        status, profile = self.req("POST", "/v1/me/profile", {"agent_type": "claude-code"}, token=buddy["token"])
        self.assertEqual(status, 200, profile)
        self.assertEqual(profile["agent_type"], "claude-code")
        error, created = self._call(buddy["token"], "diaodu_luckin_claim", {
            "city": "杭州",
            "shop_name": "瑞幸咖啡（西湖店）",
            "phone": PHONE,
            "consent": True,
        })
        self.assertFalse(error, created)
        self.assertEqual(created["drink"], "生椰拿铁")
        self.assertEqual(created["remaining"], 1)

        error, second = self._call(claude["token"], "diaodu_luckin_claim", {
            "city": "深圳", "shop_name": "瑞幸咖啡（科技园店）", "email": "claude@example.com", "consent": True,
        })
        self.assertFalse(error, second)
        self.assertEqual(second["remaining"], 0)
        error, third = self._call(grok["token"], "diaodu_luckin_claim", {
            "city": "北京", "shop_name": "瑞幸咖啡（国贸店）", "phone": PHONE_C, "drink": "latte", "consent": True,
        })
        self.assertTrue(error)
        self.assertEqual(third["reason"], "quota_full")
        self.assertEqual(self.req("GET", "/v1/luckin/quota")[1]["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
