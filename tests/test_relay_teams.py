import concurrent.futures
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from diaodu import relay_teams_mcp
from diaodu.identity import AuthError
from diaodu.relay import Relay
from diaodu.relay_teams import RelayTeams

ENVIRONMENT = {"os_name": "macOS", "os_version": "test", "architecture": "arm64",
               "shells": ["bash"], "python_version": "3.11.15"}


class TeamsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.teams = RelayTeams(self.root, max_workers=3)
        self.owner = self.agent("owner")
        self.mentor = self.agent("mentor")
        self.worker1 = self.agent("worker1")
        self.worker2 = self.agent("worker2")
        created = self.teams.create(self.owner, {"title": "团队任务", "goal": "并行执行"})
        self.sid = created["session"]["id"]
        self.mentor_code = self.invite("mentor")["code"]
        self.mentor_session = self.join(self.mentor, self.mentor_code, "mentor", environment=False)
        self.code1 = self.invite("worker")["code"]
        self.seat1 = self.join(self.worker1, self.code1)["seat"]
        self.code2 = self.invite("worker")["code"]
        self.seat2 = self.join(self.worker2, self.code2)["seat"]

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, name):
        return {"kind": "agent", "agent_id": "ag_" + name, "user_id": "person",
                "agent_name": name, "token": "never-store-" + name}

    def invite(self, role, seat=None, note="", actor=None):
        body = {"role": role, "note": note}
        if seat is not None:
            body["seat"] = seat
        return self.teams.invite(actor or self.owner, self.sid, body)["invite"]

    def join(self, actor, code, role=None, environment=True):
        body = {"code": code}
        if role:
            body["role"] = role
        if environment:
            body["environment"] = dict(ENVIRONMENT)
        return self.teams.join(actor, body)["session"]

    def send(self, actor, kind="note", text="消息", request_id="req-1", **extra):
        return self.teams.send(actor, self.sid, {"kind": kind, "text": text,
                                                 "request_id": request_id, **extra})

    def drain(self, actor, after=0):
        seen, cursor = [], after
        for _ in range(200):
            page = self.teams.events(actor, self.sid, after=cursor, wait=0)
            seen.extend(page["events"])
            if not page["has_more"]:
                return seen, page["next_cursor"]
            self.assertGreater(page["next_cursor"], cursor, "游标没有推进")
            cursor = page["next_cursor"]
        raise AssertionError("分页没有结束")

    def test_seats_are_assigned_per_role(self):
        self.assertEqual(self.mentor_session["seat"], 0)
        self.assertEqual(self.seat1, 1)
        self.assertEqual(self.seat2, 2)
        view = self.teams.get(self.owner, self.sid)["session"]
        self.assertEqual(view["mentor"]["seat"], 0)
        self.assertEqual([worker["seat"] for worker in view["workers"]], [1, 2])
        self.assertEqual([worker["name"] for worker in view["workers"]], ["worker1", "worker2"])
        self.assertNotIn("actor_key", json.dumps(view))

    def test_invite_is_single_use_and_reports_clear_reasons(self):
        code = self.invite("worker")["code"]
        self.join(self.agent("invitee1"), code)
        with self.assertRaises(AuthError) as used:
            self.join(self.agent("invitee2"), code)
        self.assertEqual(used.exception.status, 403)
        self.assertIn("已被使用", str(used.exception))
        with sqlite3.connect(self.teams.path) as db:
            db.execute("UPDATE team_invites SET expires=0 WHERE code_hash=?",
                       (hashlib.sha256(code.encode()).hexdigest(),))
        with self.assertRaises(AuthError) as expired:
            self.join(self.agent("invitee3"), code)
        self.assertEqual(expired.exception.status, 403)
        self.assertIn("已过期", str(expired.exception))
        fresh = self.invite("worker")
        self.teams.revoke_invite(self.owner, self.sid, fresh["id"])
        with self.assertRaises(AuthError) as revoked:
            self.join(self.agent("invitee4"), fresh["code"])
        self.assertEqual(revoked.exception.status, 403)
        self.assertIn("已被撤销", str(revoked.exception))

    def test_invite_ledger_records_usage_and_statuses(self):
        invite = self.invite("worker", note="Alice 的笔记本")
        self.assertEqual(invite["status"], "unused")
        self.join(self.agent("alice"), invite["code"])
        ledger = self.teams.invites(self.owner, self.sid)["invites"]
        record = next(item for item in ledger if item["id"] == invite["id"])
        self.assertEqual(record["status"], "used")
        self.assertEqual(record["note"], "Alice 的笔记本")
        self.assertEqual(record["used_by"]["name"], "alice")
        self.assertNotIn("code", record)

    def test_worker_seat_limit_and_concurrent_joins_do_not_collide(self):
        created = self.teams.create(self.owner, {"title": "并发", "goal": "抢席位"})
        sid = created["session"]["id"]
        codes = [self.teams.invite(self.owner, sid, {"role": "worker"})["invite"]["code"]
                 for _ in range(6)]
        agents = [self.agent(f"racer{i}") for i in range(6)]
        results = []
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            futures = [pool.submit(self.teams.join, agent,
                                   {"code": code, "environment": dict(ENVIRONMENT)})
                       for agent, code in zip(agents, codes)]
            for future in futures:
                try:
                    results.append(future.result()["session"]["seat"])
                except AuthError as error:
                    results.append(error.status)
        seats = sorted(seat for seat in results if seat != 409)
        self.assertEqual(seats, [1, 2, 3])
        self.assertEqual(results.count(409), 3)
        view = self.teams.get(self.owner, sid)["session"]
        self.assertEqual([worker["seat"] for worker in view["workers"]], [1, 2, 3])

    def test_specific_seat_invite_and_occupied_seat(self):
        code = self.invite("worker", seat=3)["code"]
        self.assertEqual(self.join(self.agent("seat3"), code)["seat"], 3)
        with self.assertRaises(AuthError) as occupied:
            self.teams.invite(self.owner, self.sid, {"role": "worker", "seat": 3})
        self.assertEqual(occupied.exception.status, 409)
        with self.assertRaises(AuthError):
            self.teams.invite(self.owner, self.sid, {"role": "mentor"})

    def test_broadcast_instruction_is_visible_to_everyone(self):
        sent = self.send(self.mentor, "instruction", "全体执行", "inst-all", to=None)
        for actor in (self.mentor, self.owner, self.worker1, self.worker2):
            events, _ = self.drain(actor)
            self.assertIn(sent["event"]["seq"], [event["seq"] for event in events])
        sent_all = self.send(self.mentor, "instruction", "全体执行2", "inst-all2", to="all")
        self.assertIn(sent_all["event"]["seq"],
                      [event["seq"] for event in self.drain(self.worker1)[0]])

    def test_targeted_instruction_only_reaches_target_worker_and_staff(self):
        sent = self.send(self.mentor, "instruction", "只给二号", "inst-2", to=f"worker:{self.seat2}")
        seq = sent["event"]["seq"]
        self.assertIn(seq, [event["seq"] for event in self.drain(self.worker2)[0]])
        self.assertNotIn(seq, [event["seq"] for event in self.drain(self.worker1)[0]])
        self.assertIn(seq, [event["seq"] for event in self.drain(self.mentor)[0]])
        self.assertIn(seq, [event["seq"] for event in self.drain(self.owner)[0]])

    def test_worker_results_and_notes_are_private_unless_broadcast(self):
        sent = self.send(self.worker1, "result", "一号结果", "r1")
        seq = sent["event"]["seq"]
        self.assertIn(seq, [event["seq"] for event in self.drain(self.worker1)[0]])
        self.assertIn(seq, [event["seq"] for event in self.drain(self.mentor)[0]])
        self.assertIn(seq, [event["seq"] for event in self.drain(self.owner)[0]])
        self.assertNotIn(seq, [event["seq"] for event in self.drain(self.worker2)[0]])
        broadcast = self.send(self.worker1, "result", "公开结果", "r1-all", to="all")
        self.assertIn(broadcast["event"]["seq"],
                      [event["seq"] for event in self.drain(self.worker2)[0]])
        note = self.send(self.worker2, "note", "私有备注", "n2")
        self.assertNotIn(note["event"]["seq"], [event["seq"] for event in self.drain(self.worker1)[0]])
        public_note = self.send(self.worker2, "note", "公开备注", "n2-all", to="all")
        self.assertIn(public_note["event"]["seq"],
                      [event["seq"] for event in self.drain(self.worker1)[0]])
        with self.assertRaises(AuthError) as caught:
            self.send(self.worker1, "result", "跨席塞结果", "r-cross", to=f"worker:{self.seat2}")
        self.assertEqual(caught.exception.status, 403)

    def test_cursor_advances_over_invisible_events_without_stalling(self):
        targeted = self.send(self.mentor, "instruction", "只给一号", "inst-1",
                             to=f"worker:{self.seat1}")
        visible = self.send(self.mentor, "instruction", "广播", "inst-broadcast")
        events, cursor = self.drain(self.worker2)
        sequences = [event["seq"] for event in events]
        self.assertNotIn(targeted["event"]["seq"], sequences)
        self.assertIn(visible["event"]["seq"], sequences)
        last_seq = self.teams.get(self.worker2, self.sid)["session"]["last_seq"]
        page = self.teams.events(self.worker2, self.sid, after=cursor, wait=0)
        self.assertEqual(page["events"], [])
        self.assertEqual(page["next_cursor"], last_seq)

    def test_result_cannot_reply_to_another_seats_instruction(self):
        targeted = self.send(self.mentor, "instruction", "只给一号", "inst-target",
                             to=f"worker:{self.seat1}")
        with self.assertRaises(AuthError) as caught:
            self.send(self.worker2, "result", "越权回报", "r-over",
                      reply_to=targeted["event"]["seq"])
        self.assertEqual(caught.exception.status, 403)
        broadcast = self.send(self.mentor, "instruction", "广播指令", "inst-broadcast-2")
        self.assertFalse(self.send(self.worker2, "result", "正常回报", "r-ok",
                                   reply_to=broadcast["event"]["seq"])["duplicate"])

    def test_revoking_a_seat_blocks_rejoin_and_invalidates_its_invites(self):
        # 先为 3 号空席生成定向邀请，再让别人通过任意空席邀请占掉 3 号。
        pending = self.invite("worker", seat=3)
        any_free = self.invite("worker")
        late = self.agent("late")
        self.assertEqual(self.join(late, any_free["code"])["seat"], 3)
        # 撤销 3 号席：占位者失去访问，定向邀请立即作废。
        self.teams.revoke(self.owner, self.sid, {"role": "worker", "seat": 3})
        with self.assertRaises(AuthError) as used:
            self.join(self.agent("late2"), pending["code"])
        self.assertEqual(used.exception.status, 403)
        self.assertIn("撤销", str(used.exception))
        with self.assertRaises(AuthError):
            self.teams.get(late, self.sid)
        self.teams.get(self.worker2, self.sid)
        # 撤销 1 号席后，该身份即使拿到新邀请也不能重入。
        self.teams.revoke(self.owner, self.sid, {"role": "worker", "seat": self.seat1})
        with self.assertRaises(AuthError):
            self.teams.get(self.worker1, self.sid)
        invite = self.invite("worker")
        with self.assertRaises(AuthError) as blocked:
            self.join(self.worker1, invite["code"])
        self.assertEqual(blocked.exception.status, 403)
        self.assertIn("已被撤销", str(blocked.exception))

    def test_instruction_target_must_exist_or_be_revoked(self):
        with self.assertRaises(ValueError):
            self.send(self.mentor, "instruction", "越界", "inst-bad", to="worker:9")
        with self.assertRaises(ValueError):
            self.send(self.mentor, "instruction", "空席", "inst-empty", to="worker:3")
        self.teams.revoke(self.owner, self.sid, {"role": "worker", "seat": self.seat2})
        with self.assertRaises(AuthError) as revoked:
            self.send(self.mentor, "instruction", "已撤销席", "inst-revoked",
                      to=f"worker:{self.seat2}")
        self.assertEqual(revoked.exception.status, 409)

    def test_invite_reset_and_role_mismatch(self):
        codes = [self.invite("worker")["code"] for _ in range(2)]
        self.assertEqual(self.teams.reset_invites(self.owner, self.sid)["revoked"], 2)
        for code in codes:
            with self.assertRaises(AuthError):
                self.join(self.agent("late" + code[-4:]), code)
        wrong = self.invite("worker")["code"]
        with self.assertRaises(ValueError):
            self.join(self.agent("wrong-role"), wrong, role="mentor")

    def test_request_id_dedupe_covers_target(self):
        first = self.send(self.worker1, "result", "结果", "same-id")
        self.assertTrue(self.send(self.worker1, "result", "结果", "same-id")["duplicate"])
        with self.assertRaises(AuthError):
            self.send(self.worker1, "result", "结果", "same-id", to="all")
        self.assertFalse(first["duplicate"])

    def test_team_storage_is_separate_from_one_to_one_relay(self):
        with self.teams._read() as db:
            team_tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("team_members", team_tables)
        self.assertNotIn("members", team_tables)
        relay = Relay(self.root)
        with relay._read() as db:
            relay_tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("team_members", relay_tables)
        self.assertNotIn("team_events", relay_tables)


class McpTeamToolSchema(unittest.TestCase):
    """Schema/implementation drift guard for the team MCP tool table.

    mcp.py rejects a tools/call whose arguments miss anything listed in required,
    while relay_teams_mcp.call() reads args["session"] for every tool after the
    create/join/list branches. A tool in that group that omits "session" from its
    schema is therefore impossible to call; the allowlist below is the only
    session-less group.
    """

    SESSIONLESS = {"relay_team_create", "relay_team_join", "relay_team_list"}

    def test_tools_that_need_a_session_declare_it(self):
        self.assertTrue(relay_teams_mcp.TOOLS)
        for name, _description, properties, required in relay_teams_mcp.TOOLS:
            with self.subTest(tool=name):
                self.assertTrue(name.startswith("relay_team_"))
                self.assertTrue(set(required) <= set(properties))
                if name in self.SESSIONLESS:
                    self.assertNotIn("session", required)
                else:
                    self.assertIn("session", properties)
                    self.assertIn("session", required)


if __name__ == "__main__":
    unittest.main()
