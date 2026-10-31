"""An owner mints one relay grant. An agent then manages that owner's topics.

The grant is not a browser cookie and not an agent Bearer. Topics it creates stay
owned by the minting identity, so that identity's own messages stay from_owner.
Instructions the agent sends with the grant are also from_owner, and they name
the agent in delegated_by.
"""

import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from diaodu.identity import Identity
from diaodu.library import Library
from diaodu.server import make_handler

ENVIRONMENT = {"os_name": "Android", "os_version": "14", "architecture": "arm64"}


class OwnerRelayGrant(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.library = Library(root, "/task")
        self.identity = Identity(root / "identity.sqlite3", "test-invite")
        self.smith = self.identity.register("smith", "Smith", "a-secure-password", "test-invite")
        token, csrf = self.identity.login("smith", "a-secure-password")
        self.cookie = "diaodu_session=" + token
        self.csrf = csrf
        self.ops = self.identity.connect("Hub 运维", "Cursor")
        self.phone = self.identity.connect("手机执行端", "手机")
        self.mentor = self.identity.connect("指导端", "Codex")
        self.other = self.identity.register("other", "其他人", "a-secure-password", "test-invite")
        other_token, other_csrf = self.identity.login("other", "a-secure-password")
        self.other_cookie = "diaodu_session=" + other_token
        self.other_csrf = other_csrf
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            None, self.library, self.identity, public_origin="https://www.chuhaijian.com"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def text(self, method, path):
        with urlopen(Request(self.base + path, method=method), timeout=8) as response:
            return response.read().decode()

    def req(self, method, path, body=None, cookie=None, csrf=None, bearer=None):
        headers = {"Accept": "application/json"}
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        elif cookie:
            headers["Cookie"] = cookie
            if csrf and method != "GET":
                headers["X-CSRF-Token"] = csrf
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(self.base + path, data=data, headers=headers, method=method), timeout=8) as response:
                payload = response.read()
                parsed = json.loads(payload) if payload else {}
                return response.status, parsed
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def rpc(self, name, arguments, bearer=None, cookie=None, csrf=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "diaodu_" + name, "arguments": arguments}}
        status, payload = self.req("POST", "/v1/mcp", body, cookie=cookie, csrf=csrf, bearer=bearer)
        self.assertEqual(status, 200, payload)
        result = payload["result"]
        parsed = json.loads(result["content"][0]["text"])
        return result, parsed

    def tool(self, name, arguments, bearer):
        result, parsed = self.rpc(name, arguments, bearer=bearer)
        self.assertFalse(result["isError"], parsed)
        self.assertEqual(result["structuredContent"], parsed)
        return parsed

    def mint(self, label="Hub 运维"):
        status, minted = self.req("POST", "/v1/relay/delegations", {"label": label},
                                  cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 201, minted)
        self.assertTrue(minted["token"].startswith("dg_"))
        self.assertTrue(minted["delegation"]["id"].startswith("dgr_"))
        self.assertNotIn("token_hash", json.dumps(minted["delegation"]))
        return minted

    def test_agent_creates_owner_topic_invites_worker_and_sends_as_owner(self):
        minted = self.mint()
        grant = minted["token"]
        listed = self.tool("relay_delegation_list", {}, self.ops["token"])
        self.assertEqual(listed["grants"], [])

        created = self.tool("relay_owner_create", {
            "grant": grant, "title": "手机接入", "goal": "让执行端待命"}, self.ops["token"])
        session = created["session"]
        sid = session["id"]
        self.assertTrue(session["owner"])
        self.assertEqual(session["role"], "owner")
        self.assertNotIn(grant, json.dumps(created))

        db = sqlite3.connect(self.library.relay.path)
        owner_key = db.execute("SELECT owner_key FROM sessions WHERE id=?", (sid,)).fetchone()[0]
        self.assertEqual(owner_key, "user:" + self.smith["user_id"])
        self.assertNotIn(grant.encode(), Path(self.library.relay.path).read_bytes())

        own = self.tool("relay_list", {}, self.ops["token"])
        self.assertEqual(own["sessions"], [])
        looked = self.tool("relay_owner_list", {"grant": grant, "session": sid}, self.ops["token"])
        self.assertTrue(looked["member"])
        self.assertEqual(looked["sessions"][0]["id"], sid)

        status, smith_list = self.req("GET", "/v1/relay/sessions", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in smith_list["sessions"]], [sid])

        invited = self.tool("relay_owner_invite", {"grant": grant, "session": sid, "role": "worker"}, self.ops["token"])
        self.assertEqual(invited["role"], "worker")
        self.assertTrue(invited["code"])
        joined = self.tool("relay_join", {
            "code": invited["code"], "role": "worker", "environment": ENVIRONMENT}, self.phone["token"])
        self.assertEqual(joined["session"]["id"], sid)
        self.assertEqual(joined["session"]["role"], "worker")

        sent = self.tool("relay_owner_send", {
            "grant": grant, "session": sid, "kind": "instruction", "text": "打开设置",
            "request_id": "phone-1"}, self.ops["token"])
        event = sent["event"]
        self.assertEqual(event["message"]["kind"], "instruction")
        self.assertTrue(event["from_owner"])
        self.assertEqual(event["sender"]["role"], "owner")
        self.assertEqual(event["sender"]["name"], "Smith")
        self.assertEqual(event["delegated_by"]["agent_id"], self.ops["agent_id"])
        self.assertEqual(event["delegated_by"]["name"], "Hub 运维")
        self.assertEqual(event["delegated_by"]["grant_id"], minted["delegation"]["id"])
        again = self.tool("relay_owner_send", {
            "grant": grant, "session": sid, "kind": "instruction", "text": "打开设置",
            "request_id": "phone-1"}, self.ops["token"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["event"]["seq"], event["seq"])

        status, browser = self.req("POST", f"/v1/relay/sessions/{sid}/messages", {
            "kind": "instruction", "text": "我自己来", "request_id": "smith-1"},
            cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200, browser)
        self.assertTrue(browser["event"]["from_owner"])
        self.assertNotIn("delegated_by", browser["event"])

        seen = self.tool("relay_read", {"session": sid, "after": 0, "wait": 0}, self.phone["token"])
        instructions = [item for item in seen["events"] if item.get("message", {}).get("kind") == "instruction"]
        self.assertEqual([item["from_owner"] for item in instructions], [True, True])
        self.assertEqual(instructions[0]["delegated_by"]["agent_id"], self.ops["agent_id"])
        self.assertNotIn("delegated_by", instructions[1])

        mentor = self.tool("relay_join", {"code": created["topic_code"], "role": "mentor"}, self.mentor["token"])
        self.assertEqual(mentor["session"]["role"], "mentor")
        note = self.tool("relay_send", {
            "session": sid, "kind": "instruction", "text": "指导端的指令", "request_id": "mentor-1"},
            self.mentor["token"])
        self.assertFalse(note["event"]["from_owner"])
        self.assertNotIn("delegated_by", note["event"])

        public_id = session["public_id"]
        status, public = self.req("GET", f"/v1/relay/public/conversations/{public_id}/events?after=0&wait=0")
        self.assertEqual(status, 200, public)
        blob = json.dumps(public, ensure_ascii=False)
        delegated = [item for item in public["events"] if item.get("delegated_by")]
        self.assertTrue(delegated)
        self.assertEqual(set(delegated[0]["delegated_by"]), {"name"})
        self.assertEqual(delegated[0]["delegated_by"]["name"], "Hub 运维")
        self.assertNotIn(self.ops["agent_id"], blob)
        self.assertNotIn(grant, blob)
        self.assertNotIn(invited["code"], blob)

        audit_blob = " ".join(row[0] for row in db.execute("SELECT detail FROM grant_audit"))
        self.assertNotIn(grant, audit_blob)
        self.assertNotIn(invited["code"], audit_blob)
        self.assertTrue(db.execute(
            "SELECT 1 FROM grant_audit WHERE action='send' AND owner_key=? AND actor_key=?",
            ("user:" + self.smith["user_id"], "agent:" + self.ops["agent_id"])).fetchone())

        status, grants = self.req("GET", "/v1/relay/delegations", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(grants["grants"][0]["last_actor_name"], "Hub 运维")
        self.assertNotIn(grant, json.dumps(grants))
        actions = [item["action"] for item in grants["audit"]]
        self.assertIn("create", actions)
        self.assertIn("invite", actions)
        self.assertIn("send", actions)

    def test_grant_is_revocable_scoped_and_not_a_bearer(self):
        minted = self.mint()
        grant = minted["token"]
        status, missing = self.req("POST", "/v1/relay/delegations", {"label": "x"}, cookie=self.cookie)
        self.assertEqual(status, 403, missing)
        status, foreign = self.req("GET", "/v1/relay/delegations", cookie=self.other_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(foreign["grants"], [])
        status, stolen = self.req("POST", f"/v1/relay/delegations/{minted['delegation']['id']}/revoke", {},
                                  cookie=self.other_cookie, csrf=self.other_csrf)
        self.assertEqual(status, 404, stolen)

        status, me = self.req("GET", "/v1/me", bearer=grant)
        self.assertEqual(status, 401, me)
        status, agents = self.req("GET", "/v1/agents", bearer=grant)
        self.assertEqual(status, 401, agents)
        status, revoked_identity = self.req(
            "POST", "/v1/agents/" + self.ops["agent_id"] + "/revoke", {}, bearer=grant)
        self.assertEqual(status, 401, revoked_identity)

        denied, parsed = self.rpc("relay_owner_create", {
            "grant": grant, "title": "人员登录不能代发", "goal": "拒绝"}, cookie=self.cookie, csrf=self.csrf)
        self.assertTrue(denied["isError"])
        self.assertEqual(parsed["code"], 403)

        extra, parsed = self.rpc("relay_create", {
            "title": "旧工具", "goal": "不接受 grant", "grant": grant}, bearer=self.ops["token"])
        self.assertTrue(extra["isError"])
        self.assertEqual(parsed["code"], 400)
        self.assertNotIn(grant, json.dumps(parsed))

        own = self.tool("relay_create", {"title": "运维自己的", "goal": "不是 Smith"}, self.ops["token"])
        db = sqlite3.connect(self.library.relay.path)
        own_key = db.execute("SELECT owner_key FROM sessions WHERE id=?", (own["session"]["id"],)).fetchone()[0]
        self.assertEqual(own_key, "agent:" + self.ops["agent_id"])
        self.assertNotEqual(own_key, "user:" + self.smith["user_id"])

        spoof, parsed = self.rpc("relay_send", {
            "session": own["session"]["id"], "kind": "note", "text": "伪造",
            "request_id": "spoof", "from_owner": True, "delegated_by": {"name": "Smith"}},
            bearer=self.ops["token"])
        self.assertTrue(spoof["isError"])
        self.assertEqual(parsed["code"], 400)

        team, parsed = self.rpc("relay_team_invite", {
            "session": "tm_nope", "role": "worker", "grant": grant}, bearer=self.ops["token"])
        self.assertTrue(team["isError"])
        self.assertEqual(parsed["code"], 400)

        self.identity.revoke(self.ops["user_id"], self.ops["agent_id"])
        status, dead = self.req("POST", "/v1/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "diaodu_relay_owner_create", "arguments": {
                "grant": grant, "title": "已撤销的实例", "goal": "拒绝"}}}, bearer=self.ops["token"])
        self.assertEqual(status, 401, dead)
        self.assertNotIn(grant, json.dumps(dead))

        fresh = self.identity.connect("另一个运维", "Cursor")
        created = self.tool("relay_owner_create", {
            "grant": grant, "title": "撤销前", "goal": "还能"}, fresh["token"])
        status, gone = self.req("POST", f"/v1/relay/delegations/{minted['delegation']['id']}/revoke", {},
                                cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200, gone)
        self.assertTrue(gone["delegation"]["revoked"])
        blocked, parsed = self.rpc("relay_owner_invite", {
            "grant": grant, "session": created["session"]["id"], "role": "worker"}, bearer=fresh["token"])
        self.assertTrue(blocked["isError"])
        self.assertEqual(parsed["code"], 401)
        self.assertNotIn(grant, parsed["message"])
        status, still = self.req("POST", f"/v1/relay/sessions/{created['session']['id']}/messages", {
            "kind": "note", "text": "所有者自己仍可发", "request_id": "still"},
            cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200, still)
        self.assertTrue(still["event"]["from_owner"])
        self.assertNotIn("delegated_by", still["event"])

        rotated = self.tool("relay_owner_topic_code", {
            "grant": self.mint("轮换")["token"], "session": created["session"]["id"]}, fresh["token"])
        self.assertNotEqual(rotated["code"], created["topic_code"])
        bad, parsed = self.rpc("relay_join", {
            "code": created["topic_code"], "role": "worker", "environment": ENVIRONMENT}, bearer=self.phone["token"])
        self.assertTrue(bad["isError"])
        self.assertEqual(parsed["code"], 403)

    def test_mcp_mint_while_signed_in_and_live_grant_cap(self):
        result, minted = self.rpc("relay_delegation_create", {"label": "MCP 签发"},
                                  cookie=self.cookie, csrf=self.csrf)
        self.assertFalse(result["isError"], minted)
        self.assertTrue(minted["token"].startswith("dg_"))
        self.assertNotIn(minted["token"], json.dumps(minted["delegation"]))

        for index in range(7):
            status, _row = self.req("POST", "/v1/relay/delegations", {"label": "n" + str(index)},
                                    cookie=self.cookie, csrf=self.csrf)
            self.assertEqual(status, 201)
        status, full = self.req("POST", "/v1/relay/delegations", {"label": "ninth"},
                                cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 429, full)

        guide = self.text("GET", "/llms.txt")
        self.assertIn("diaodu_relay_owner_create", guide)
        self.assertIn("diaodu_relay_owner_invite", guide)
        page = self.text("GET", "/v1/agent-guide.md")
        self.assertIn("签发授权", page)
        self.assertIn("diaodu_relay_owner_invite", page)
        self.assertIn("from_owner", page)

    def test_grant_survives_restart(self):
        minted = self.mint("重启")
        from diaodu.relay import Relay
        reopened = Relay(self.library.root)
        created = reopened.create(
            {"kind": "user", "user_id": self.smith["user_id"], "display_name": "Smith"},
            {"title": "直接", "goal": "对照"})
        self.assertEqual(created["session"]["role"], "owner")
        owner, delegation = reopened.open_delegation(minted["token"], {
            "kind": "agent", "agent_id": self.ops["agent_id"], "user_id": self.ops["user_id"],
            "agent_name": "Hub 运维"})
        made = reopened.create(owner, {"title": "重启后", "goal": "仍归 Smith"}, delegation=delegation)
        db = sqlite3.connect(reopened.path)
        key = db.execute("SELECT owner_key FROM sessions WHERE id=?", (made["session"]["id"],)).fetchone()[0]
        self.assertEqual(key, "user:" + self.smith["user_id"])
        self.assertNotEqual(made["session"]["id"], created["session"]["id"])


if __name__ == "__main__":
    unittest.main()
