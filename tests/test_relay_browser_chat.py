"""The workbench talks to agents with the same cookie the browser sends.

These tests follow the page: create or join a 1:1 Topic, create or join a team,
send instruction / progress / result / question, and keep codes out of public JSON.
"""

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from diaodu.identity import Identity, digest
from diaodu.library import Library
from diaodu.server import make_handler

ENVIRONMENT = {"os_name": "Linux", "os_version": "test", "architecture": "x86_64"}


class BrowserRelayChat(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.library = Library(root, "/task")
        self.identity = Identity(root / "identity.sqlite3")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            None, self.library, self.identity, public_origin="https://www.chuhaijian.com"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.human = self.identity.connect("项目负责人", "浏览器", "human")
        self.worker = self.identity.connect("执行端", "测试Agent")
        self.other = self.identity.connect("另一位", "浏览器", "human")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def headers(self, actor=None, bearer=None):
        head = {"Accept": "application/json"}
        if bearer is not None:
            head["Authorization"] = "Bearer " + bearer["token"]
        elif actor is not None:
            head["Cookie"] = "diaodu_session=" + actor["token"]
            head["X-CSRF-Token"] = digest("csrf:" + actor["token"])
        return head

    def req(self, method, path, body=None, actor=None, bearer=None, raw=None, content_type=None):
        head = self.headers(actor, bearer)
        if raw is not None:
            data = raw
            if content_type:
                head["Content-Type"] = content_type
        elif body is not None:
            data = json.dumps(body).encode()
            head["Content-Type"] = "application/json"
        else:
            data = None
        try:
            with urlopen(Request(self.base + path, data=data, headers=head, method=method), timeout=8) as response:
                payload = response.read()
                kind = response.headers.get("Content-Type", "")
                parsed = json.loads(payload) if "application/json" in kind and payload else payload
                return response.status, parsed
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_person_exchanges_a_one_to_one_thread_and_can_join_a_seat(self):
        status, created = self.req("POST", "/v1/relay/sessions", {
            "title": "排查部署", "goal": "请执行端回报检查结果"}, actor=self.human)
        self.assertEqual(status, 201)
        session = created["session"]
        code = created["topic_code"]
        self.assertNotIn("topic_code", session)
        self.assertNotIn(code, json.dumps(session))

        status, joined = self.req("POST", "/v1/relay/join", {
            "code": code, "role": "worker", "environment": dict(ENVIRONMENT)}, bearer=self.worker)
        self.assertEqual(status, 200, joined)
        self.assertEqual(joined["session"]["role"], "worker")

        status, instruction = self.req("POST", f"/v1/relay/sessions/{session['id']}/messages", {
            "kind": "instruction", "text": "检查服务是否在监听", "request_id": "browser-instruction"}, actor=self.human)
        self.assertEqual(status, 200, instruction)
        seq = instruction["event"]["seq"]
        status, question = self.req("POST", f"/v1/relay/sessions/{session['id']}/messages", {
            "kind": "question", "text": "需要我补充日志吗？", "request_id": "browser-question"}, actor=self.human)
        self.assertEqual(status, 200, question)
        status, progress = self.req("POST", f"/v1/relay/sessions/{session['id']}/messages", {
            "kind": "progress", "text": "正在检查端口", "request_id": "worker-progress", "reply_to": seq}, bearer=self.worker)
        self.assertEqual(status, 200, progress)
        status, result = self.req("POST", f"/v1/relay/sessions/{session['id']}/messages", {
            "kind": "result", "text": "8765 端口正在监听", "request_id": "worker-result", "reply_to": seq}, bearer=self.worker)
        self.assertEqual(status, 200, result)

        status, thread = self.req("GET", f"/v1/relay/sessions/{session['id']}/events?after=0&wait=0", actor=self.human)
        self.assertEqual(status, 200)
        kinds = [event["message"]["kind"] for event in thread["events"] if event["kind"] == "message"]
        self.assertEqual(kinds, ["instruction", "question", "progress", "result"])
        texts = [event["message"]["text"] for event in thread["events"] if event["kind"] == "message"]
        self.assertEqual(texts[0], "检查服务是否在监听")
        self.assertEqual(texts[-1], "8765 端口正在监听")

        status, mentor = self.req("POST", "/v1/relay/join", {"code": code, "role": "mentor"}, actor=self.other)
        self.assertEqual(status, 200, mentor)
        self.assertEqual(mentor["session"]["role"], "mentor")
        status, note = self.req("POST", f"/v1/relay/sessions/{session['id']}/messages", {
            "kind": "instruction", "text": "补充：只读检查，不要重启", "request_id": "mentor-instruction"}, actor=self.other)
        self.assertEqual(status, 200, note)

        status, upload = self.req(
            "POST", f"/v1/relay/sessions/{session['id']}/files?name=note.txt",
            actor=self.human, raw=b"listener ok", content_type="text/plain")
        self.assertEqual(status, 201, upload)
        status, attached = self.req("POST", f"/v1/relay/sessions/{session['id']}/messages", {
            "kind": "note", "text": "附上检查记录", "request_id": "browser-file",
            "attachments": [upload["id"]]}, actor=self.human)
        self.assertEqual(status, 200, attached)
        self.assertEqual(attached["event"]["message"]["attachments"][0]["name"], "note.txt")

        status, opened = self.req("POST", f"/v1/relay/sessions/{session['id']}/public", {"enabled": True}, actor=self.human)
        self.assertEqual(status, 200)
        token = opened["public_url"].rstrip("/").rsplit("/", 1)[-1]
        status, public = self.req("GET", f"/v1/relay/public/{token}/events?after=0&wait=0")
        self.assertEqual(status, 200)
        blob = json.dumps(public, ensure_ascii=False)
        self.assertIn("检查服务是否在监听", blob)
        self.assertNotIn(code, blob)
        self.assertNotIn('"environment"', blob)
        self.assertNotIn("topic_code", blob)
        self.assertNotIn("actor_key", blob)
        self.assertNotIn(self.human["token"], blob)

    def test_person_runs_a_team_thread_with_broadcast_and_one_seat(self):
        status, created = self.req("POST", "/v1/relay/teams", {
            "title": "并行排查", "goal": "两个执行端分别回报"}, actor=self.human)
        self.assertEqual(status, 201)
        sid = created["session"]["id"]
        self.assertTrue(sid.startswith("tm_"))

        status, first = self.req("POST", f"/v1/relay/teams/{sid}/invites", {"role": "worker", "seat": 1, "note": "给甲"}, actor=self.human)
        self.assertEqual(status, 201)
        status, second = self.req("POST", f"/v1/relay/teams/{sid}/invites", {"role": "worker", "seat": 2, "note": "给乙"}, actor=self.human)
        self.assertEqual(status, 201)
        code_a = first["invite"]["code"]
        code_b = second["invite"]["code"]
        self.assertNotEqual(code_a, code_b)

        status, ledger = self.req("GET", f"/v1/relay/teams/{sid}/invites", actor=self.human)
        self.assertEqual(status, 200)
        self.assertNotIn(code_a, json.dumps(ledger))
        self.assertNotIn(code_b, json.dumps(ledger))

        status, joined = self.req("POST", "/v1/relay/teams/join", {
            "code": code_a, "role": "worker", "environment": dict(ENVIRONMENT)}, actor=self.other)
        self.assertEqual(status, 200, joined)
        self.assertEqual(joined["session"]["seat"], 1)
        status, again = self.req("POST", "/v1/relay/teams/join", {
            "code": code_a, "role": "worker", "environment": dict(ENVIRONMENT)}, bearer=self.worker)
        self.assertEqual(status, 403, again)
        status, seat2 = self.req("POST", "/v1/relay/teams/join", {
            "code": code_b, "environment": dict(ENVIRONMENT)}, bearer=self.worker)
        self.assertEqual(status, 200, seat2)
        self.assertEqual(seat2["session"]["seat"], 2)

        status, broadcast = self.req("POST", f"/v1/relay/teams/{sid}/messages", {
            "kind": "instruction", "text": "全体检查自己的环境", "request_id": "team-all"}, actor=self.human)
        self.assertEqual(status, 200, broadcast)
        status, directed = self.req("POST", f"/v1/relay/teams/{sid}/messages", {
            "kind": "instruction", "text": "只给 2 号席：核对端口", "request_id": "team-seat-2",
            "to": "worker:2"}, actor=self.human)
        self.assertEqual(status, 200, directed)

        status, seen_by_one = self.req("GET", f"/v1/relay/teams/{sid}/events?after=0&wait=0", actor=self.other)
        texts = [event.get("message", {}).get("text") for event in seen_by_one["events"]]
        self.assertIn("全体检查自己的环境", texts)
        self.assertNotIn("只给 2 号席：核对端口", texts)
        status, seen_by_two = self.req("GET", f"/v1/relay/teams/{sid}/events?after=0&wait=0", bearer=self.worker)
        texts = [event.get("message", {}).get("text") for event in seen_by_two["events"]]
        self.assertIn("全体检查自己的环境", texts)
        self.assertIn("只给 2 号席：核对端口", texts)

        status, reply = self.req("POST", f"/v1/relay/teams/{sid}/messages", {
            "kind": "result", "text": "2 号席端口正常", "request_id": "seat-2-result",
            "reply_to": directed["event"]["seq"]}, bearer=self.worker)
        self.assertEqual(status, 200, reply)
        status, owner_view = self.req("GET", f"/v1/relay/teams/{sid}/events?after=0&wait=0", actor=self.human)
        messages = [event for event in owner_view["events"] if event["kind"] == "message"]
        self.assertEqual(messages[-1]["message"]["text"], "2 号席端口正常")
        self.assertEqual(messages[-1]["sender"]["seat"], 2)
        self.assertEqual(messages[-1]["message"]["reply_to"], directed["event"]["seq"])

        status, opened = self.req("POST", f"/v1/relay/teams/{sid}/public", {"enabled": True}, actor=self.human)
        token = opened["public_url"].rstrip("/").rsplit("/", 1)[-1]
        status, public = self.req("GET", f"/v1/relay/public/{token}/events?after=0&wait=0")
        self.assertEqual(status, 200)
        blob = json.dumps(public, ensure_ascii=False)
        self.assertIn("全体检查自己的环境", blob)
        self.assertNotIn(code_a, blob)
        self.assertNotIn(code_b, blob)
        self.assertNotIn('"environment"', blob)
        self.assertNotIn("invite_code", blob)
        self.assertNotIn(self.human["token"], blob)


if __name__ == "__main__":
    unittest.main()
