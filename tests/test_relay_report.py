"""Final report: long Markdown attachment, public md/pdf, and the conversation page."""
import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from shutil import which
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from diaodu.identity import AuthError, Identity
from diaodu.library import Library
from diaodu.relay_teams import RelayTeams
from diaodu.report_md import blocks_to_html, collect_headings, inline_html, number_headings, parse_blocks
from diaodu.report_pdf import render_html
from diaodu.server import make_handler

ENVIRONMENT = {"os_name": "macOS", "os_version": "test", "architecture": "arm64",
               "shells": ["bash"], "python_version": "3.11.15"}

SAMPLE = """# 沿海小城周末集市调研

调研时间：2026 年 10 月。执行席分别走访了早市、夜市和社区团购群，本报告只保留能够复核的数字。

## 结论

周末集市仍然是家庭采购的主要补充，但客单和复购已经明显分化。早市靠时令蔬菜，夜市靠熟食，社区团购靠次日达。

## 价格对照

| 渠道 | 采样天数 | 晚饭成本（元） | 复购 | 备注 |
| --- | --- | --- | --- | --- |
| 早市 | 6 | 28 | 高 | 叶菜新鲜，雨天摊位少 |
| 夜市 | 6 | 46 | 中 | 熟食占比高，等待约 15 分钟 |
| 社区团购 | 6 | 33 | 高 | 次日达，缺货会改派 |

## 现场记录

1. 早市 6:40 前叶菜价差最大，过了 8:00 只剩处理价。
2. 夜市两家摊位用同一家中央厨房，口味差主要来自等待时间。
3. 团购群里的缺货会改派，不应算进失败率。

详细说明见 [公开说明](https://example.com/market-note)。

<script>alert(1)</script>

## 建议

- 把早市作为默认采购渠道，夜市只保留熟食。
- 下一轮补上雨天样本，再决定是否调整库存。

```
$ echo 不要把整段当代码折叠
```
"""


class MarkdownTests(unittest.TestCase):
    def test_tables_links_and_raw_html_stay_text(self):
        blocks = number_headings(parse_blocks(SAMPLE))
        html = blocks_to_html(blocks)
        self.assertIn("<table>", html)
        self.assertIn("<td>早市</td>", html)
        self.assertIn('<a href="https://example.com/market-note">公开说明</a>', html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("javascript:", html)
        headings = collect_headings(blocks)
        self.assertGreaterEqual(len(headings), 3)
        self.assertTrue(all(item["id"].startswith("sec-") for item in headings))
        self.assertEqual(inline_html(""), "")
        poisoned = blocks_to_html(parse_blocks("[点我](javascript:alert(1))\n\n[好的](https://example.com/a)"))
        self.assertNotIn("javascript:", poisoned)
        self.assertIn('href="https://example.com/a"', poisoned)

    def test_print_html_has_cover_toc_and_page_numbers(self):
        html = render_html(markdown=SAMPLE, title="沿海小城周末集市调研", team_title="集市小组",
                           author="执行 1 号席 · 小李", when="2026-10-26 18:00", chars=1200)
        self.assertIn("最终报告", html)
        self.assertIn("目录", html)
        self.assertIn("counter(page)", html)
        self.assertIn("沿海小城周末集市调研", html)
        self.assertIn("<th>晚饭成本（元）</th>", html)
        self.assertNotIn("lease_until", html)
        self.assertNotIn("actor_key", html)
        self.assertNotIn("<script", html.lower())


class ReportStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.teams = RelayTeams(self.root, max_workers=3,
                                public_base_path="/task", public_origin="https://example.test")
        self.owner = self.agent("owner")
        self.mentor = self.agent("mentor")
        self.worker1 = self.agent("worker1")
        self.worker2 = self.agent("worker2")
        created = self.teams.create(self.owner, {"title": "集市小组", "goal": "周末集市"})
        self.sid = created["session"]["id"]
        self.public_id = created["session"]["public_id"]
        self.code = self.teams.invite(self.owner, self.sid, {"role": "mentor"})["invite"]["code"]
        self.teams.join(self.mentor, {"code": self.code, "role": "mentor"})
        self.seat1 = self.teams.join(self.worker1, {
            "code": self.teams.invite(self.owner, self.sid, {"role": "worker"})["invite"]["code"],
            "role": "worker", "environment": dict(ENVIRONMENT)})["session"]["seat"]
        self.seat2 = self.teams.join(self.worker2, {
            "code": self.teams.invite(self.owner, self.sid, {"role": "worker"})["invite"]["code"],
            "role": "worker", "environment": dict(ENVIRONMENT)})["session"]["seat"]
        self.parent = self.teams.create_task(self.owner, self.sid, {
            "title": "汇总报告", "request_id": "parent"})["task"]

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, name):
        return {"kind": "agent", "agent_id": "ag_" + name, "user_id": "person",
                "agent_name": name, "token": "never-store-" + name}

    def test_leader_or_staff_can_deliver_a_long_report_in_chunks(self):
        with self.assertRaises(AuthError) as denied:
            self.teams.submit_report(self.worker2, self.sid, {
                "markdown": "不该成功", "request_id": "nope"})
        self.assertEqual(denied.exception.status, 403)
        first = "甲" * 200_000
        opened = self.teams.submit_report(self.owner, self.sid, {
            "title": "沿海小城周末集市调研", "markdown": first, "request_id": "part-1",
            "task_id": self.parent["id"]})
        self.assertFalse(opened["duplicate"])
        self.assertEqual(opened["report"]["chars"], 200_000)
        self.assertEqual(opened["report"]["markdown"], first)
        self.assertTrue(opened["report"]["file_id"].startswith("fl_"))
        again = self.teams.submit_report(self.owner, self.sid, {
            "title": "沿海小城周末集市调研", "markdown": first, "request_id": "part-1",
            "task_id": self.parent["id"]})
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["report"]["id"], opened["report"]["id"])
        with self.assertRaises(AuthError):
            self.teams.submit_report(self.owner, self.sid, {
                "markdown": "另一份", "request_id": "part-1"})
        second = "乙" * 200_000
        appended = self.teams.submit_report(self.mentor, self.sid, {
            "append": second, "request_id": "part-2"})
        self.assertEqual(appended["report"]["chars"], 400_000)
        self.assertEqual(appended["report"]["markdown"], first + second)
        self.assertEqual(appended["report"]["task_id"], self.parent["id"])
        self.assertEqual(appended["report"]["revision"], 2)
        with self.assertRaises(ValueError):
            self.teams.submit_report(self.owner, self.sid, {
                "append": "丙", "request_id": "too-big"})
        kept = self.teams.read_report(self.worker1, self.sid)["report"]
        self.assertEqual(kept["chars"], 400_000)
        self.assertEqual(kept["markdown"], first + second)
        with sqlite3.connect(self.teams.path) as db:
            names = [row[0] for row in db.execute(
                "SELECT name FROM team_files WHERE session_id=?", (self.sid,))]
            kinds = [row[0] for row in db.execute(
                "SELECT type FROM team_files WHERE session_id=?", (self.sid,))]
            current = db.execute(
                "SELECT COUNT(*) FROM team_reports WHERE session_id=? AND status='current'",
                (self.sid,)).fetchone()[0]
        self.assertEqual(names, ["report.md", "report.md"])
        self.assertEqual(kinds, ["text/markdown", "text/markdown"])
        self.assertEqual(current, 1)
        board = self.teams.list_tasks(self.worker2, self.sid)
        self.assertEqual(board["tasks"][0]["report"]["chars"], 400_000)
        self.assertNotIn("markdown", board["tasks"][0]["report"])
        self.assertNotIn("file_id", board["tasks"][0]["report"])
        self.teams.appoint(self.owner, self.sid, {"role": "worker", "seat": self.seat1})
        replaced = self.teams.submit_report(self.worker1, self.sid, {
            "markdown": SAMPLE, "title": "沿海小城周末集市调研", "request_id": "final",
            "task_id": self.parent["id"]})
        self.assertEqual(replaced["report"]["author"]["seat"], self.seat1)
        self.assertNotIn("actor_key", replaced["report"])
        self.assertNotIn("lease_until", replaced["report"])

    def test_public_markdown_and_pdf_hide_credentials(self):
        self.teams.submit_report(self.owner, self.sid, {
            "markdown": SAMPLE, "title": "沿海小城周末集市调研", "request_id": "public",
            "task_id": self.parent["id"]})
        failed = self.teams.send(self.worker1, self.sid, {
            "kind": "result", "text": "失败：超时，未能取到夜市价格", "request_id": "bad",
            "task_id": self.parent["id"], "outcome": "failed"})
        self.assertEqual(failed["event"]["message"]["outcome"], "failed")
        detail = self.teams.directory_conversation(self.public_id)
        self.assertTrue(detail["report_available"])
        self.assertEqual(set(detail["report"]), {
            "available", "title", "chars", "revision", "task_id", "updated_at", "author",
            "markdown_url", "pdf_url"})
        self.assertEqual(set(detail["report"]["author"]), {"role", "seat", "name"})
        self.assertTrue(detail["report"]["markdown_url"].endswith("/report.md"))
        self.assertNotIn(self.sid, detail["report"]["markdown_url"])
        self.assertNotIn("sig=", detail["report"]["pdf_url"])
        listed = next(item for _, _, item in self.teams.directory_candidates(20)
                      if item["public_id"] == self.public_id)
        self.assertTrue(listed["report_available"])
        self.assertNotIn("report", listed)
        self.assertNotIn("file_id", listed)
        meta, path = self.teams.public_report_path(self.public_id, "markdown")
        self.assertEqual(path.read_text(encoding="utf-8"), SAMPLE)
        self.assertEqual(meta["name"], "report.md")
        pdf_meta, pdf_path = self.teams.public_report_path(self.public_id, "pdf")
        pdf = pdf_path.read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(pdf_meta["size"], 1000)
        cached, cached_path = self.teams.public_report_path(self.public_id, "pdf")
        self.assertEqual(cached_path, pdf_path)
        self.assertEqual(cached["size"], pdf_meta["size"])
        page = self.teams.directory_events(self.public_id, 0, 50)
        blob = json.dumps({"detail": detail, "listed": listed, "events": page}, ensure_ascii=False)
        self.assertNotIn(self.code, blob)
        self.assertNotIn(self.sid, blob)
        self.assertNotIn("actor_key", blob)
        self.assertNotIn("lease_until", blob)
        self.assertNotIn("never-store-", blob)
        self.assertNotIn("file_id", blob)
        self.assertNotIn("sha256", blob)
        self.assertNotIn(self.code.encode(), pdf)
        self.assertNotIn(self.sid.encode(), pdf)
        self.assertNotIn(b"lease_until", pdf)
        self.assertNotIn(b"actor_key", pdf)
        self.assertNotIn(b"never-store-", pdf)
        if which("pdftotext"):
            extracted = subprocess.check_output(["pdftotext", "-layout", str(pdf_path), "-"], timeout=30)
            text = extracted.decode("utf-8", "replace")
            self.assertIn("价格对照", text)
            self.assertIn("早市", text)
            self.assertIn("建议", text)
            self.assertNotIn(self.code, text)
        script = Path(__file__).resolve().parent.parent.joinpath("static", "conversations.js").read_text()
        self.assertIn("final-report", script)
        self.assertIn("下载 PDF", script)
        self.assertIn("查看 MD", script)
        self.assertIn("report-available", script)
        self.assertIn("renderMarkdown", script)
        self.assertIn("已被后续结果取代", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("lease_until", script)
        self.teams.state(self.owner, self.sid, {"status": "closed"})
        self.assertEqual(self.teams.public_report_path(self.public_id, "markdown")[1].read_text(encoding="utf-8"), SAMPLE)
        with self.assertRaises(AuthError) as closed:
            self.teams.submit_report(self.owner, self.sid, {"markdown": "太晚", "request_id": "late"})
        self.assertEqual(closed.exception.status, 409)


class ReportHTTPTests(unittest.TestCase):
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
        self.actors = {name: self.identity.connect(name, "测试Agent") for name in ("owner", "worker")}
        status, created, _ = self.req("POST", "/v1/relay/teams", {"title": "演示", "goal": "交付"}, "owner")
        self.assertEqual(status, 201)
        self.sid = created["session"]["id"]
        self.public_id = created["session"]["public_id"]
        invite = self.req("POST", f"/v1/relay/teams/{self.sid}/invites", {"role": "worker"}, "owner")[1]["invite"]
        self.code = invite["code"]
        joined = self.req("POST", "/v1/relay/teams/join",
                          {"code": self.code, "role": "worker", "environment": dict(ENVIRONMENT)}, "worker")
        self.assertEqual(joined[0], 200)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def req(self, method, path, body=None, role=None):
        headers = {"Content-Type": "application/json"}
        if role:
            headers["Authorization"] = "Bearer " + self.actors[role]["token"]
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urlopen(Request(self.base + path, data=data, headers=headers, method=method), timeout=60) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type:
                    return response.status, json.loads(raw.decode()), content_type
                return response.status, raw, content_type
        except HTTPError as error:
            with error:
                raw = error.read()
                content_type = error.headers.get("Content-Type", "")
                if "json" in content_type:
                    return error.code, json.loads(raw.decode()), content_type
                return error.code, raw, content_type

    def test_public_routes_and_a_body_past_the_message_cap(self):
        self.assertEqual(self.req("GET", f"/v1/relay/public/conversations/{self.public_id}/report.md")[0], 404)
        denied = self.req("POST", f"/v1/relay/teams/{self.sid}/report",
                          {"markdown": "执行端不能交最终报告", "request_id": "worker"}, "worker")
        self.assertEqual(denied[0], 403)
        body = "# 长报告\n\n" + ("乙" * 90_000)
        status, saved, _ = self.req("POST", f"/v1/relay/teams/{self.sid}/report", {
            "title": "长报告", "markdown": body, "request_id": "http-1"}, "owner")
        self.assertEqual(status, 200, saved)
        self.assertGreater(saved["report"]["chars"], 90_000)
        status, listed, _ = self.req("GET", "/v1/relay/public/conversations?limit=20")
        card = next(item for item in listed["items"] if item["public_id"] == self.public_id)
        self.assertTrue(card["report_available"])
        status, markdown, content_type = self.req(
            "GET", f"/v1/relay/public/conversations/{self.public_id}/report.md")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", content_type)
        self.assertEqual(markdown.decode("utf-8"), body)
        self.assertNotIn(self.code.encode(), markdown)
        tools = self.req("POST", "/v1/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "owner")
        names = {item["name"] for item in tools[1]["result"]["tools"]}
        self.assertIn("diaodu_relay_team_report", names)
        self.assertIn("diaodu_relay_team_report_read", names)
        call = self.req("POST", "/v1/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "diaodu_relay_team_report_read", "arguments": {"session": self.sid}}}, "owner")
        self.assertEqual(call[0], 200)
        self.assertFalse(call[1]["result"]["isError"])
        payload = json.loads(call[1]["result"]["content"][0]["text"])
        self.assertEqual(payload["report"]["markdown"], body)
        self.assertNotIn("lease_until", json.dumps(payload))
        self.assertNotIn(self.code, json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
