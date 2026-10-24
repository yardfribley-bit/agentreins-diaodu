"""Public conversation directory: both stores, legacy rows, and credential redaction."""

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from diaodu import relay_http
from diaodu.identity import Identity
from diaodu.library import Library
from diaodu.relay import Relay
from diaodu.server import make_handler


ENVIRONMENT = {"os_name": "Ubuntu-directory-test", "os_version": "24.04", "architecture": "x86_64",
               "shells": ["bash"], "python_version": "3.12.9"}
FORBIDDEN_KEYS = {
    "environment", "actor_key", "owner_key", "sender_key", "topic_code", "topic_code_hash",
    "public_token", "public_token_hash", "public_url", "sig", "signature", "download_url",
    "session_id", "bearer", "code_hash", "used_by_key", "id",
}


def _keys(value, found):
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)


class DirectoryMigration(unittest.TestCase):
    def test_legacy_private_rows_are_listed_without_codes(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        topic = "legacy-topic-PLAINTEXT-code"
        actor_key = "user:legacy-secret-owner"
        self._legacy_pair(root / "relay.sqlite3", topic, actor_key)
        self._legacy_team(root / "relay_teams.sqlite3")

        library = Library(root, "/task")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            None, library, public_origin="https://www.chuhaijian.com"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # addCleanup runs last-registered first: shutdown, then join, then close.
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_address[1])

        with urlopen(base + "/v1/relay/public/conversations?limit=20", timeout=10) as response:
            listed = json.load(response)
        titles = {item["title"]: item for item in listed["items"]}
        self.assertIn("旧的私有对话", titles)
        self.assertIn("旧团队对话", titles)
        pair = titles["旧的私有对话"]
        team = titles["旧团队对话"]
        self.assertEqual(pair["mode"], "pair")
        self.assertEqual(team["mode"], "team")
        self.assertRegex(pair["public_id"], r"^C-p[0-9a-f]{8}$")
        self.assertRegex(team["public_id"], r"^C-t[0-9a-f]{8}$")
        self.assertGreater(team["updated_at"], pair["updated_at"])
        self.assertEqual(listed["items"][0]["public_id"], team["public_id"])

        with urlopen(base + "/v1/relay/public/conversations/" + pair["public_id"] + "/events?wait=0",
                     timeout=10) as response:
            events = json.load(response)
        texts = [event.get("message", {}).get("text") for event in events["events"]]
        self.assertIn("legacy hello", texts)
        blob = json.dumps({"listed": listed, "events": events}, ensure_ascii=False)
        for secret in (topic, hashlib.sha256(topic.encode()).hexdigest(), actor_key,
                       "rl_legacyprivate", "tm_legacyprivate"):
            self.assertNotIn(secret, blob)
        found = set()
        _keys(listed, found)
        _keys(events, found)
        self.assertFalse(found & FORBIDDEN_KEYS)

        db = sqlite3.connect(root / "relay.sqlite3")
        row = db.execute("SELECT visibility, public_id FROM sessions WHERE id='rl_legacyprivate'").fetchone()
        db.close()
        self.assertEqual(row[0], "public")
        self.assertEqual(row[1], pair["public_id"])

    def _legacy_pair(self, path, topic, actor_key):
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL,
              status TEXT NOT NULL, owner_key TEXT NOT NULL,
              created REAL NOT NULL, updated REAL NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
              topic_code_hash TEXT, public_read INTEGER NOT NULL DEFAULT 0,
              public_token_hash TEXT);
            CREATE TABLE members (
              session_id TEXT NOT NULL, role TEXT NOT NULL, actor_key TEXT NOT NULL,
              name TEXT NOT NULL, last_seen REAL NOT NULL, ack_seq INTEGER NOT NULL DEFAULT 0,
              environment TEXT, PRIMARY KEY(session_id,role), UNIQUE(session_id,actor_key));
            CREATE TABLE events (
              session_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
              created REAL NOT NULL, sender_role TEXT NOT NULL, sender_name TEXT NOT NULL,
              payload TEXT NOT NULL, sender_key TEXT, request_id TEXT, content_hash TEXT,
              PRIMARY KEY(session_id,seq), UNIQUE(session_id,sender_key,request_id));
        """)
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   ("rl_legacyprivate", "旧的私有对话", "只有当时的成员", "active", actor_key,
                    10, 10, 1, hashlib.sha256(topic.encode()).hexdigest(), 0, None))
        db.execute("INSERT INTO members VALUES (?,?,?,?,?,?,?)",
                   ("rl_legacyprivate", "owner", actor_key, "旧管理者", 10, 0, None))
        payload = json.dumps({"message": {"kind": "note", "reply_to": None, "text": "legacy hello"}},
                             ensure_ascii=False, sort_keys=True)
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("rl_legacyprivate", 1, "message", 11, "owner", "旧管理者", payload,
                    actor_key, "legacy-req", "hash"))
        db.commit()
        db.close()

    def _legacy_team(self, path):
        db = sqlite3.connect(path)
        db.execute("""
            CREATE TABLE team_sessions (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL,
              status TEXT NOT NULL, owner_key TEXT NOT NULL,
              created REAL NOT NULL, updated REAL NOT NULL,
              last_seq INTEGER NOT NULL DEFAULT 0,
              public_read INTEGER NOT NULL DEFAULT 0, public_token_hash TEXT)
        """)
        db.execute("INSERT INTO team_sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("tm_legacyprivate", "旧团队对话", "旧的团队目标", "active",
                    "user:legacy-team-owner", 20, 30, 0, 0, None))
        db.commit()
        db.close()


class PublicDirectory(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.library = Library(root, "/task")
        self.identity = Identity(root / "identity.sqlite3")
        self.actors = {name: self.identity.connect(name, "测试Agent")
                       for name in ("owner", "mentor", "worker")}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            None, self.library, self.identity, public_origin="https://www.chuhaijian.com"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])
        with relay_http._HITS_LOCK:
            relay_http._DIRECTORY_HITS.clear()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temporary.cleanup()

    def req(self, method, path, body=None, role=None, timeout=10):
        headers = {"Content-Type": "application/json"}
        if role:
            headers["Authorization"] = "Bearer " + self.actors[role]["token"]
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urlopen(Request(self.base + path, data=data, headers=headers, method=method), timeout=timeout) as response:
                raw = response.read()
                if "application/json" in response.headers.get("Content-Type", ""):
                    return response.status, json.loads(raw)
                return response.status, raw
        except HTTPError as error:
            with error:
                payload = error.read()
                try:
                    return error.code, json.loads(payload)
                except json.JSONDecodeError:
                    return error.code, payload

    def anon(self, path, timeout=10):
        return self.req("GET", path, timeout=timeout)

    def assert_clean(self, payload, secrets):
        blob = json.dumps(payload, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, blob)
        found = set()
        _keys(payload, found)
        self.assertFalse(found & FORBIDDEN_KEYS, found & FORBIDDEN_KEYS)

    def test_new_conversations_are_public_and_codes_stay_out(self):
        goal = "目" * 200
        status, created = self.req("POST", "/v1/relay/sessions",
                                   {"title": "目录一对一", "goal": goal}, "owner")
        self.assertEqual(status, 201)
        pair_id = created["session"]["id"]
        public_pair = created["session"]["public_id"]
        topic = created["topic_code"]
        self.assertEqual(created["session"]["visibility"], "public")
        self.assertRegex(public_pair, r"^C-p[0-9a-f]{8}$")
        self.assertEqual(self.req("POST", "/v1/relay/join", {"code": topic, "role": "mentor"}, "mentor")[0], 200)
        self.assertEqual(self.req("POST", "/v1/relay/join",
                                  {"code": topic, "role": "worker", "environment": dict(ENVIRONMENT)},
                                  "worker")[0], 200)
        changed = dict(ENVIRONMENT, python_version="3.12.10")
        self.req("POST", f"/v1/relay/sessions/{pair_id}/heartbeat", {"environment": changed}, "worker")
        content = b"\x89PNG\r\n\x1a\n" + b"public-bytes" * 20
        upload = Request(self.base + f"/v1/relay/sessions/{pair_id}/files?name=" + quote("目录图.png"),
                         data=content, method="POST",
                         headers={"Content-Type": "application/octet-stream",
                                  "Authorization": "Bearer " + self.actors["worker"]["token"]})
        with urlopen(upload, timeout=10) as response:
            meta = json.load(response)
        self.req("POST", f"/v1/relay/sessions/{pair_id}/messages",
                 {"kind": "note", "text": "带附件的公开备注", "request_id": "dir-1",
                  "attachments": [meta["id"]]}, "mentor")

        time.sleep(0.02)
        status, team = self.req("POST", "/v1/relay/teams",
                                {"title": "目录团队", "goal": "一起做"}, "owner")
        self.assertEqual(status, 201)
        team_sid = team["session"]["id"]
        public_team = team["session"]["public_id"]
        self.assertEqual(team["session"]["visibility"], "public")
        self.assertRegex(public_team, r"^C-t[0-9a-f]{8}$")
        status, invite = self.req("POST", f"/v1/relay/teams/{team_sid}/invites",
                                  {"role": "worker"}, "owner")
        self.assertEqual(status, 201)
        invite_code = invite["invite"]["code"]
        joined = self.req("POST", "/v1/relay/teams/join",
                          {"code": invite_code, "environment": dict(ENVIRONMENT)}, "worker")
        self.assertEqual(joined[0], 200)
        seat = joined[1]["session"]["seat"]
        directed = "只给这个席位的指令-紫丁香"
        self.assertEqual(self.req("POST", f"/v1/relay/teams/{team_sid}/messages",
                                  {"kind": "instruction", "text": directed, "request_id": "dir-team",
                                   "to": f"worker:{seat}"}, "owner")[0], 200)
        team_bytes = b"\x89PNG\r\n\x1a\n" + b"team-bytes" * 10
        team_upload = Request(self.base + f"/v1/relay/teams/{team_sid}/files?name=" + quote("团队图.png"),
                              data=team_bytes, method="POST",
                              headers={"Content-Type": "application/octet-stream",
                                       "Authorization": "Bearer " + self.actors["owner"]["token"]})
        with urlopen(team_upload, timeout=10) as response:
            team_meta = json.load(response)
        self.req("POST", f"/v1/relay/teams/{team_sid}/messages",
                 {"kind": "note", "text": "团队附件", "request_id": "dir-team-file",
                  "attachments": [team_meta["id"]]}, "owner")

        status, listed = self.anon("/v1/relay/public/conversations?limit=10")
        self.assertEqual(status, 200)
        order = [item["public_id"] for item in listed["items"]]
        self.assertEqual(order[0], public_team)
        self.assertIn(public_pair, order)
        pair_item = next(item for item in listed["items"] if item["public_id"] == public_pair)
        self.assertEqual(len(pair_item["goal"]), 140)
        self.assertTrue(pair_item["goal"].endswith("…"))
        self.assertEqual(pair_item["last_message"]["kind"], "note")
        self.assertIn("带附件", pair_item["last_message"]["preview"])
        self.assertNotIn("seat", pair_item["participants"][0])

        status, detail = self.anon("/v1/relay/public/conversations/" + public_pair)
        self.assertEqual(status, 200)
        self.assertEqual(detail["conversation"]["goal"], goal)
        self.assertEqual(detail["conversation"]["mode"], "pair")

        status, events = self.anon(f"/v1/relay/public/conversations/{public_pair}/events?after=0&limit=50&wait=0")
        self.assertEqual(status, 200)
        kinds = [event["kind"] for event in events["events"]]
        self.assertIn("message", kinds)
        self.assertNotIn("environment", kinds)
        attachments = [item for event in events["events"] for item in (event.get("message", {}).get("attachments") or [])]
        self.assertTrue(attachments)
        self.assertEqual(set(attachments[0]), {"name", "size", "type", "url"})
        self.assertNotIn(pair_id, attachments[0]["url"])
        self.assertNotIn("sig=", attachments[0]["url"])
        downloaded = self.raw_get(self.local(attachments[0]["url"]))
        self.assertEqual(downloaded[0], 200)
        self.assertEqual(downloaded[1], content)

        status, team_events = self.anon(f"/v1/relay/public/conversations/{public_team}/events?wait=0")
        self.assertEqual(status, 200)
        messages = [event["message"] for event in team_events["events"] if event["kind"] == "message"]
        directed_message = next(message for message in messages if message["text"] == directed)
        self.assertEqual(directed_message["to"], f"worker:{seat}")
        team_files = [item for message in messages for item in message.get("attachments") or []]
        self.assertEqual(self.raw_get(self.local(team_files[0]["url"]))[1], team_bytes)

        status, pairs_only = self.anon("/v1/relay/public/conversations?mode=pair")
        self.assertEqual(status, 200)
        self.assertEqual([item["mode"] for item in pairs_only["items"]], ["pair"])
        status, open_only = self.anon("/v1/relay/public/conversations?status=open")
        self.assertEqual(status, 200)
        self.assertTrue(open_only["items"])

        secrets = [topic, invite_code, pair_id, team_sid,
                   self.actors["owner"]["token"], self.actors["worker"]["token"],
                   self.actors["owner"]["agent_id"], self.actors["owner"]["user_id"],
                   "Ubuntu-directory-test", "3.12.10", "bash"]
        # File ids are part of the public download URL on purpose. Codes and session ids are not.
        for payload in (listed, detail, events, team_events, pairs_only):
            self.assert_clean(payload, secrets)
        self.assertNotIn(meta["id"], json.dumps(detail))

        member = self.req("GET", f"/v1/relay/sessions/{pair_id}/events?wait=0", role="mentor")
        self.assertIn("environment", [event["kind"] for event in member[1]["events"]])
        self.assertEqual(self.anon(f"/v1/relay/sessions/{pair_id}")[0], 401)
        self.assertEqual(self.req("POST", "/v1/relay/public/conversations", {})[0], 401)
        started = time.monotonic()
        self.assertEqual(self.anon(f"/v1/relay/public/conversations/{public_pair}/events?wait=20", timeout=3)[0], 400)
        self.assertLess(time.monotonic() - started, 2)

        status, enabled = self.req("POST", f"/v1/relay/sessions/{pair_id}/public", {"enabled": True}, "owner")
        token = enabled["public_url"].rsplit("/", 1)[-1]
        watch = self.anon(f"/v1/relay/public/{quote(token, safe='')}/events?wait=0")
        watch_files = [item for event in watch[1]["events"]
                       for item in (event.get("message", {}).get("attachments") or [])]
        self.assertEqual(set(watch_files[0]), {"name", "size", "type"})

        page = urlopen(self.base + "/conversations/", timeout=10)
        html = page.read().decode()
        self.assertIn("对话", html)
        self.assertIn("AgentReins Hub", html)
        self.assertNotIn(topic, html)
        script = (Path(__file__).resolve().parent.parent / "static" / "conversations.js").read_text()
        self.assertIn("wait=0", script)
        self.assertNotIn("wait=20", script)
        self.assertNotIn("innerHTML", script)

    def test_pagination_and_private_visibility(self):
        ids = []
        for index in range(3):
            status, created = self.req("POST", "/v1/relay/sessions",
                                       {"title": f"分页{index}", "goal": "翻页"}, "owner")
            self.assertEqual(status, 201)
            ids.append(created["session"]["public_id"])
            time.sleep(0.01)
        seen = []
        cursor = None
        for _ in range(5):
            path = "/v1/relay/public/conversations?limit=2"
            if cursor:
                path += "&cursor=" + quote(cursor, safe="")
            status, page = self.anon(path)
            self.assertEqual(status, 200)
            self.assertLessEqual(len(page["items"]), 2)
            seen.extend(item["public_id"] for item in page["items"])
            if not page["has_more"]:
                cursor = None
                break
            cursor = page["next_cursor"]
            self.assertTrue(cursor)
        self.assertIsNone(cursor)
        self.assertEqual(seen, list(reversed(ids)))

        hidden = ids[0]
        db = sqlite3.connect(self.library.relay.path)
        db.execute("UPDATE sessions SET visibility='private' WHERE public_id=?", (hidden,))
        db.commit()
        db.close()
        Relay(self.library.root, "/task", "https://www.chuhaijian.com")
        self.assertEqual(self.anon("/v1/relay/public/conversations/" + hidden)[0], 404)
        status, again = self.anon("/v1/relay/public/conversations?limit=10")
        self.assertNotIn(hidden, [item["public_id"] for item in again["items"]])
        sid = self.req("GET", "/v1/relay/sessions", role="owner")[1]["sessions"]
        match = next(session for session in sid if session["public_id"] == hidden)
        self.assertEqual(self.req("GET", "/v1/relay/sessions/" + match["id"], role="owner")[0], 200)
        still = sqlite3.connect(self.library.relay.path)
        visibility = still.execute("SELECT visibility FROM sessions WHERE public_id=?", (hidden,)).fetchone()[0]
        still.close()
        self.assertEqual(visibility, "private")

    def test_closing_a_session_filters_but_stays_readable(self):
        status, created = self.req("POST", "/v1/relay/sessions",
                                   {"title": "会结束", "goal": "结束后仍公开"}, "owner")
        sid = created["session"]["id"]
        public_id = created["session"]["public_id"]
        self.req("POST", f"/v1/relay/sessions/{sid}/messages",
                 {"kind": "note", "text": "结束前的话", "request_id": "bye"}, "owner")
        self.assertEqual(self.req("POST", f"/v1/relay/sessions/{sid}/state",
                                  {"status": "closed"}, "owner")[0], 200)
        status, closed = self.anon("/v1/relay/public/conversations?status=closed")
        self.assertIn(public_id, [item["public_id"] for item in closed["items"]])
        status, ongoing = self.anon("/v1/relay/public/conversations?status=open")
        self.assertNotIn(public_id, [item["public_id"] for item in ongoing["items"]])
        status, events = self.anon(f"/v1/relay/public/conversations/{public_id}/events?wait=0")
        self.assertEqual(events["conversation"]["status"], "closed")
        self.assertIn("结束前的话", [event.get("message", {}).get("text") for event in events["events"]])

    def raw_get(self, url):
        try:
            with urlopen(url, timeout=10) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def local(self, url):
        path = urlparse(url).path
        if path.startswith("/task/"):
            path = path[len("/task"):]
        return self.base + path


if __name__ == "__main__":
    unittest.main()
