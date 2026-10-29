"""Partner pull feed: owner approval, revoke, and anonymous reads."""
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from diaodu.identity import Identity
from diaodu.library import Library
from diaodu.report_public import review
from diaodu.server import make_handler

ENVIRONMENT = {"os_name": "Linux", "os_version": "test", "architecture": "x86_64",
               "shells": ["bash"], "python_version": "3.11.15"}
MARKDOWN = """# 行业地图

本报告收录 83 个公开项目，只保留可以复核的名称和链接。

## 范围

项目分布在安全工具、协议实现和社区维护三类。正文不包含访问凭证。
"""
SECRET = "da_THIS_IS_NOT_A_REAL_TOKEN_VALUE"
EMAIL = "map-contact@example.com"


class ReviewTests(unittest.TestCase):
    def test_tokens_block_and_emails_only_warn(self):
        blocking, warnings = review("联系 map-contact@example.com 即可。")
        self.assertEqual(blocking, [])
        self.assertTrue(warnings)
        self.assertNotIn(EMAIL, " ".join(warnings))
        blocking, warnings = review("凭证 " + SECRET + " 不能出现。")
        self.assertEqual(blocking, ["访问令牌"])
        self.assertNotIn(SECRET, " ".join(blocking))
        blocking, _warnings = review("内部 ag_" + "ab" * 8 + " 以及 /relay/join/abc 和 actor_key")
        self.assertIn("内部编号", blocking)
        self.assertIn("邀请或围观链接", blocking)
        self.assertIn("凭证字段", blocking)
        self.assertNotIn("访问令牌", blocking)
        self.assertNotIn("ag_", " ".join(blocking))


class PublicReportFeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.library = Library(root, "/task")
        self.identity = Identity(root / "identity.sqlite3", "")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            None, self.library, self.identity, public_origin="https://www.chuhaijian.com"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.actors = {name: self.identity.connect(name, "测试Agent")
                       for name in ("owner", "mentor", "worker", "stranger")}
        status, created, _headers = self.req("POST", "/v1/relay/teams",
                                             {"title": "行业地图", "goal": "83 个项目"}, "owner")
        self.assertEqual(status, 201, created)
        self.sid = created["session"]["id"]
        self.public_id = created["session"]["public_id"]
        self.assertTrue(self.public_id.startswith("C-t"))
        mentor_code = self.req("POST", "/v1/relay/teams/" + self.sid + "/invites",
                               {"role": "mentor"}, "owner")[1]["invite"]["code"]
        worker_invite = self.req("POST", "/v1/relay/teams/" + self.sid + "/invites",
                                 {"role": "worker"}, "owner")[1]["invite"]
        self.code = worker_invite["code"]
        self.assertEqual(self.req("POST", "/v1/relay/teams/join",
                                  {"code": mentor_code, "role": "mentor"}, "mentor")[0], 200)
        self.assertEqual(self.req("POST", "/v1/relay/teams/join", {
            "code": self.code, "role": "worker", "environment": dict(ENVIRONMENT)}, "worker")[0], 200)
        posted = self.req("POST", "/v1/relay/teams/" + self.sid + "/report", {
            "title": "行业地图", "markdown": MARKDOWN, "request_id": "map"}, "owner")
        self.assertEqual(posted[0], 200, posted[1])
        self.report_sha = hashlib.sha256(MARKDOWN.encode()).hexdigest()
        self.assertEqual(posted[1]["report"]["sha256"], self.report_sha)
        self.assertFalse(posted[1]["report"]["publication"]["approved"])
        self.assertIsNone(posted[1]["report"]["publication"]["public_approved_at"])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def req(self, method, path, body=None, role=None, headers=None):
        status, payload, content_type, response_headers = self.raw(method, path, body, role, headers)
        if "json" in content_type or (payload[:1] in (b"{", b"[")):
            try:
                return status, json.loads(payload.decode()), response_headers
            except json.JSONDecodeError:
                return status, payload, response_headers
        return status, payload, response_headers

    def raw(self, method, path, body=None, role=None, headers=None):
        merged = {"Content-Type": "application/json"}
        if headers:
            merged.update(headers)
        if role:
            merged["Authorization"] = "Bearer " + self.actors[role]["token"]
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, data=data, headers=merged, method=method)
        try:
            with urlopen(request, timeout=60) as response:
                return response.status, response.read(), response.headers.get("Content-Type", ""), response.headers
        except HTTPError as error:
            with error:
                return error.code, error.read(), error.headers.get("Content-Type", ""), error.headers

    def feed(self, headers=None):
        return self.req("GET", "/v1/public/reports.json", headers=headers)

    def partner(self, suffix):
        return self.raw("GET", "/v1/public/reports/" + self.public_id + "/" + suffix)

    def test_approval_feed_revoke_and_privacy(self):
        status, listed, headers = self.feed()
        self.assertEqual(status, 200)
        self.assertEqual(listed["reports"], [])
        self.assertIsNone(listed["updated_at"])
        self.assertIn("ETag", headers)
        self.assertNotIn("Authorization", headers)
        untouched = self.partner("report.md")
        self.assertEqual(untouched[0], 404)
        self.assertNotIn(MARKDOWN.encode()[:20], untouched[1])
        self.assertEqual(self.partner("report.pdf")[0], 404)
        # The conversation page keeps serving the report without partner approval.
        conversation = self.raw("GET", "/v1/relay/public/conversations/" + self.public_id + "/report.md")
        self.assertEqual(conversation[0], 200)
        self.assertEqual(conversation[1].decode(), MARKDOWN)

        denied = self.req("POST", "/v1/relay/teams/" + self.public_id + "/report/publication",
                          {"approved": True}, "worker")
        self.assertEqual(denied[0], 403, denied[1])
        self.assertNotIn(self.code, json.dumps(denied[1]))
        self.assertNotIn(self.actors["worker"]["token"], json.dumps(denied[1]))
        mentor = self.req("POST", "/v1/relay/teams/" + self.sid + "/report/publication",
                          {"approved": True}, "mentor")
        self.assertEqual(mentor[0], 403, mentor[1])
        stranger = self.req("POST", "/v1/relay/teams/" + self.public_id + "/report/publication",
                            {"approved": True}, "stranger")
        self.assertEqual(stranger[0], 404, stranger[1])
        anonymous = self.req("POST", "/v1/relay/teams/" + self.public_id + "/report/publication",
                             {"approved": True})
        self.assertEqual(anonymous[0], 401, anonymous[1])
        self.assertEqual(self.feed()[1]["reports"], [])

        blocked = self.req("POST", "/v1/relay/teams/" + self.sid + "/report", {
            "markdown": MARKDOWN + "\n\n" + SECRET, "request_id": "secret"}, "owner")
        self.assertEqual(blocked[0], 200, blocked[1])
        refused = self.req("POST", "/v1/relay/teams/" + self.public_id + "/report/publication",
                           {"approved": True}, "owner")
        self.assertEqual(refused[0], 400, refused[1])
        self.assertNotIn(SECRET, json.dumps(refused[1]))
        self.assertIn("访问令牌", refused[1]["error"])
        self.assertEqual(self.feed()[1]["reports"], [])
        self.assertEqual(self.partner("report.md")[0], 404)

        restored = self.req("POST", "/v1/relay/teams/" + self.sid + "/report", {
            "title": "行业地图", "markdown": MARKDOWN, "request_id": "clean"}, "owner")
        self.assertEqual(restored[0], 200, restored[1])
        self.assertFalse(restored[1]["report"]["publication"]["approved"])

        call = self.req("POST", "/v1/mcp", {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "diaodu_relay_team_report_publish", "arguments": {
                "session": self.public_id, "approved": True,
                "title": "行业地图", "summary": "83 个公开项目"}}}, "owner")
        self.assertEqual(call[0], 200, call[1])
        self.assertFalse(call[1]["result"]["isError"], call[1])
        published = json.loads(call[1]["result"]["content"][0]["text"])
        publication = published["publication"]
        self.assertTrue(publication["approved"])
        self.assertTrue(publication["public_approved_at"].endswith("Z"))
        self.assertEqual(publication["sha256"], self.report_sha)
        self.assertEqual(publication["warnings"], [])
        worker_call = self.req("POST", "/v1/mcp", {
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "diaodu_relay_team_report_publish", "arguments": {
                "session": self.public_id, "approved": False}}}, "worker")
        self.assertTrue(worker_call[1]["result"]["isError"])

        status, listed, headers = self.feed()
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["reports"]), 1)
        item = listed["reports"][0]
        self.assertEqual(set(item), {
            "id", "title", "summary", "created_at", "updated_at", "pdf_url", "md_url",
            "conversation_url", "sha256", "public_approved_at"})
        self.assertEqual(set(listed), {"reports", "updated_at"})
        base = "https://www.chuhaijian.com/task"
        self.assertEqual(item["id"], self.public_id)
        self.assertEqual(item["title"], "行业地图")
        self.assertEqual(item["summary"], "83 个公开项目")
        self.assertEqual(item["sha256"], self.report_sha)
        self.assertEqual(item["public_approved_at"], publication["public_approved_at"])
        self.assertEqual(item["md_url"], base + "/v1/public/reports/" + self.public_id + "/report.md")
        self.assertEqual(item["pdf_url"], base + "/v1/public/reports/" + self.public_id + "/report.pdf")
        self.assertEqual(item["conversation_url"], base + "/conversations/" + self.public_id + "/")
        for value in item.values():
            self.assertNotIn(self.sid, str(value))
            self.assertNotIn(self.code, str(value))
            self.assertNotIn(self.actors["owner"]["token"], str(value))
            self.assertNotIn(self.actors["owner"]["agent_id"], str(value))
        blob = json.dumps(listed)
        self.assertNotIn(EMAIL, blob)
        self.assertNotIn("actor_key", blob)
        self.assertNotIn("lease_until", blob)
        self.assertNotIn("file_id", blob)
        self.assertNotIn(SECRET, blob)

        status, markdown, content_type, file_headers = self.partner("report.md")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", content_type)
        self.assertEqual(markdown.decode(), MARKDOWN)
        self.assertEqual(file_headers["ETag"], '"' + self.report_sha + '"')
        self.assertNotIn(self.code.encode(), markdown)
        self.assertNotIn(self.sid.encode(), markdown)
        self.assertNotIn(self.actors["owner"]["token"].encode(), markdown)
        again = self.raw("GET", "/v1/public/reports.json", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(again[0], 304)
        self.assertEqual(again[1], b"")
        self.assertEqual(again[3]["ETag"], headers["ETag"])

        pdf_status, pdf, pdf_type, _pdf_headers = self.partner("report.pdf")
        self.assertEqual(pdf_status, 200, pdf[:200])
        self.assertIn("application/pdf", pdf_type)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertNotIn(self.code.encode(), pdf)
        self.assertNotIn(self.sid.encode(), pdf)
        self.assertNotIn(self.actors["owner"]["agent_id"].encode(), pdf)

        warned = self.req("POST", "/v1/relay/teams/" + self.public_id + "/report/publication",
                          {"approved": True, "summary": "公开联系 " + EMAIL}, "owner")
        self.assertEqual(warned[0], 200, warned[1])
        self.assertEqual(warned[1]["publication"]["public_approved_at"], publication["public_approved_at"])
        self.assertTrue(warned[1]["publication"]["warnings"])
        self.assertNotIn(EMAIL, " ".join(warned[1]["publication"]["warnings"]))
        self.assertEqual(self.feed()[1]["reports"][0]["summary"], "公开联系 " + EMAIL)

        revised = self.req("POST", "/v1/relay/teams/" + self.sid + "/report", {
            "markdown": MARKDOWN + "\n\n补充：仍不公开。\n", "request_id": "revise"}, "owner")
        self.assertEqual(revised[0], 200, revised[1])
        self.assertFalse(revised[1]["report"]["publication"]["approved"])
        self.assertEqual(self.feed()[1]["reports"], [])
        self.assertEqual(self.partner("report.md")[0], 404)
        self.assertEqual(self.partner("report.pdf")[0], 404)

        republished = self.req("POST", "/v1/relay/teams/" + self.public_id + "/report/publication",
                               {"approved": True}, "owner")
        self.assertEqual(republished[0], 200, republished[1])
        self.assertNotEqual(republished[1]["publication"]["public_approved_at"],
                            publication["public_approved_at"])
        self.assertEqual(len(self.feed()[1]["reports"]), 1)

        revoked = self.req("POST", "/v1/relay/teams/" + self.sid + "/report/publication",
                           {"approved": False}, "owner")
        self.assertEqual(revoked[0], 200, revoked[1])
        self.assertFalse(revoked[1]["publication"]["approved"])
        self.assertIsNone(revoked[1]["publication"]["public_approved_at"])
        self.assertNotIn("md_url", revoked[1]["publication"])
        status, listed, _headers = self.feed()
        self.assertEqual(listed["reports"], [])
        self.assertIsNotNone(listed["updated_at"])
        self.assertEqual(self.partner("report.md")[0], 404)
        self.assertEqual(self.partner("report.pdf")[0], 404)
        self.assertNotIn(self.sid.encode(), self.partner("report.md")[1])
        # Existing conversation downloads stay available after revoke.
        self.assertEqual(self.raw(
            "GET", "/v1/relay/public/conversations/" + self.public_id + "/report.md")[0], 200)

        guide = self.raw("GET", "/llms.txt")
        self.assertEqual(guide[0], 200)
        self.assertIn(b"https://www.chuhaijian.com/task/v1/public/reports.json", guide[1])
        spec = self.req("GET", "/v1/openapi.json")
        self.assertEqual(spec[0], 200)
        self.assertIn("/public/reports.json", spec[1]["paths"])
        self.assertIn("/relay/teams/{session}/report/publication", spec[1]["paths"])
        names = {item["name"] for item in self.req("POST", "/v1/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "owner")[1]["result"]["tools"]}
        self.assertIn("diaodu_relay_team_report_publish", names)


if __name__ == "__main__":
    unittest.main()
