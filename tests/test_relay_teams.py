import concurrent.futures
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_waiter_cap_is_per_session_and_releases_on_abandon_or_error(self):
        self.assertEqual((self.teams.MAX_WAITERS, self.teams.MAX_SESSION_WAITERS), (256, 32))
        with patch.dict(os.environ, {"DIAODU_RELAY_MAX_WAITERS": "9", "DIAODU_RELAY_SESSION_WAITERS": "4"}):
            tuned = RelayTeams(self.root / "tuned", max_workers=3)
        self.assertEqual((tuned.MAX_WAITERS, tuned.MAX_SESSION_WAITERS), (9, 4))
        self.teams.MAX_WAITERS = 8
        self.teams.MAX_SESSION_WAITERS = 1
        cursor = self.teams.get(self.worker1, self.sid)["session"]["last_seq"]
        other = self.teams.create(self.owner, {"title": "另一队", "goal": "隔离"})["session"]["id"]
        stop = threading.Event()

        def abandon():
            return stop.is_set()

        def wait_for(count):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self.teams._condition:
                    if len(self.teams._waiters) == count:
                        return
                time.sleep(0.005)
            self.fail("long poll did not register")

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            try:
                first = pool.submit(self.teams.events, self.worker1, self.sid, cursor, 20, abandon)
                wait_for(1)
                with self.assertRaises(AuthError) as caught:
                    self.teams.events(self.worker2, self.sid, cursor, 1)
                self.assertEqual(caught.exception.status, 429)
                self.assertIn("本会话实时等待已达上限", str(caught.exception))
                self.assertIn("全局剩余 7/8", str(caught.exception))
                second = pool.submit(self.teams.events, self.owner, other, 0, 20, abandon)
                wait_for(2)
                held = {item[0] for item in self.teams._waiters}
                self.assertEqual(held, {self.sid, other})
                stop.set()
                with self.teams._condition:
                    self.teams._condition.notify_all()
                self.assertEqual(first.result(timeout=2)["events"], [])
                self.assertEqual(second.result(timeout=2)["events"], [])
            finally:
                stop.set()
                with self.teams._condition:
                    self.teams._condition.notify_all()
        self.assertEqual(self.teams._waiters, set())

        real_page = self.teams._page
        state = {"armed": False}

        def flaky(*args, **kwargs):
            if state["armed"]:
                raise RuntimeError("store failed")
            return real_page(*args, **kwargs)

        with patch.object(self.teams, "_page", side_effect=flaky):
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                waiting = pool.submit(self.teams.events, self.mentor, self.sid, cursor, 20)
                wait_for(1)
                state["armed"] = True
                with self.teams._condition:
                    self.teams._condition.notify_all()
                with self.assertRaises(RuntimeError):
                    waiting.result(timeout=2)
        self.assertEqual(self.teams._waiters, set())

    def test_team_storage_is_separate_from_one_to_one_relay(self):
        with self.teams._read() as db:
            team_tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("team_members", team_tables)
        self.assertIn("team_gates", team_tables)
        self.assertIn("team_gate_uses", team_tables)
        self.assertNotIn("members", team_tables)
        relay = Relay(self.root)
        with relay._read() as db:
            relay_tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("team_members", relay_tables)
        self.assertNotIn("team_events", relay_tables)
        self.assertNotIn("team_gates", relay_tables)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.teams = RelayTeams(self.root, max_workers=8)
        self.owner = self.agent("owner")
        self.mentor = self.agent("mentor")
        created = self.teams.create(self.owner, {"title": "门禁任务", "goal": "一次配对多名执行端"})
        self.sid = created["session"]["id"]
        self.public_id = created["session"]["public_id"]

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, name):
        return {"kind": "agent", "agent_id": "ag_" + name, "user_id": "person",
                "agent_name": name, "token": "never-store-" + name}

    def open(self, role="worker", max_joins=2, ttl_hours=None, note="", actor=None):
        body = {"role": role, "max_joins": max_joins, "note": note}
        if ttl_hours is not None:
            body["ttl_hours"] = ttl_hours
        return self.teams.open_gate(actor or self.owner, self.sid, body)["gate"]

    def enter(self, actor, code, environment=True, role=None, client_ip=None):
        body = {"gate_code": code}
        if role is not None:
            body["role"] = role
        if environment:
            body["environment"] = dict(ENVIRONMENT)
        return self.teams.join_gate(actor, body, client_ip=client_ip)["session"]

    def test_short_code_alphabet_and_one_line_hint(self):
        gate = self.open(max_joins=5, note="周五一批")
        code = gate["code"]
        self.assertEqual(len(code), RelayTeams.GATE_CODE_LENGTH)
        self.assertEqual(RelayTeams.GATE_CODE_LENGTH, 6)
        self.assertTrue(set(code) <= set(RelayTeams.GATE_CODE_ALPHABET))
        self.assertTrue(set("01ILlOo").isdisjoint(RelayTeams.GATE_CODE_ALPHABET))
        self.assertEqual(gate["remaining"], 5)
        self.assertEqual(gate["max_joins"], 5)
        self.assertEqual(gate["role"], "worker")
        self.assertEqual(gate["status"], "open")
        self.assertEqual(gate["session_id"], self.sid)
        self.assertEqual(gate["public_id"], self.public_id)
        self.assertNotIn("\n", gate["join_hint"])
        self.assertEqual(
            gate["join_hint"],
            "diaodu_relay_team_gate_join gate_code=%s environment={os_name,os_version,architecture}" % code)
        self.assertNotIn("```", gate["join_hint"])

    def test_code_is_hashed_at_rest_and_absent_from_events(self):
        gate = self.open()
        code = gate["code"]
        self.enter(self.agent("w1"), code)
        with sqlite3.connect(self.teams.path) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(team_gates)")]
            self.assertNotIn("code", columns)
            stored = db.execute("SELECT code_hash, note FROM team_gates").fetchone()
            self.assertEqual(stored[0], hashlib.sha256(code.encode()).hexdigest())
            self.assertNotEqual(stored[0], code)
            self.assertNotIn(code, stored[1])
            payloads = [row[0] for row in db.execute("SELECT payload FROM team_events")]
        self.assertTrue(payloads)
        self.assertTrue(all(code not in payload for payload in payloads))
        view = self.teams.get(self.owner, self.sid)["session"]
        self.assertNotIn(code, json.dumps(view))

    def test_n_joins_then_the_next_fails(self):
        gate = self.open(max_joins=5)
        seats = []
        for index in range(1, 6):
            code = "  " + gate["code"].lower() + " " if index == 1 else gate["code"]
            seats.append(self.enter(self.agent("w%d" % index), code)["seat"])
        self.assertEqual(seats, [1, 2, 3, 4, 5])
        with self.assertRaises(AuthError) as full:
            self.enter(self.agent("w6"), gate["code"])
        self.assertEqual(full.exception.status, 403)
        self.assertIn("名额已用完", str(full.exception))
        status = self.teams.gate_status(self.owner, self.sid, "worker")["gate"]
        self.assertEqual(status["status"], "exhausted")
        self.assertEqual(status["remaining"], 0)
        self.assertEqual(status["used"], 5)
        self.assertEqual([item["name"] for item in status["used_by"]],
                         ["w1", "w2", "w3", "w4", "w5"])
        self.assertEqual([item["seat"] for item in status["used_by"]], [1, 2, 3, 4, 5])
        self.assertNotIn("code", status)
        self.assertNotIn("join_hint", status)
        self.assertNotIn("actor_key", json.dumps(status))
        self.assertNotIn(gate["code"], json.dumps(status))

    def test_close_and_expiry_stop_joins(self):
        gate = self.open(max_joins=3)
        self.teams.close_gate(self.owner, self.sid, {})
        with self.assertRaises(AuthError) as closed:
            self.enter(self.agent("w1"), gate["code"])
        self.assertEqual(closed.exception.status, 403)
        self.assertIn("已关闭", str(closed.exception))
        again = self.teams.close_gate(self.owner, self.sid, {"role": "worker"})
        self.assertTrue(again["closed"])
        self.assertEqual(again["gate"]["status"], "closed")
        self.assertNotIn("code", again["gate"])

        fresh = self.open(max_joins=2)
        with sqlite3.connect(self.teams.path) as db:
            db.execute("UPDATE team_gates SET expires=0 WHERE id=?", (fresh["id"],))
        with self.assertRaises(AuthError) as expired:
            self.enter(self.agent("w2"), fresh["code"])
        self.assertEqual(expired.exception.status, 403)
        self.assertIn("已过期", str(expired.exception))
        self.assertEqual(self.teams.gate_status(self.owner, self.sid, "worker")["gate"]["status"],
                         "expired")

    def test_rotate_keeps_remaining_and_retires_the_old_code(self):
        gate = self.open(max_joins=3)
        self.enter(self.agent("w1"), gate["code"])
        rotated = self.teams.rotate_gate(self.owner, self.sid, {"role": "worker"})["gate"]
        self.assertNotEqual(rotated["code"], gate["code"])
        self.assertEqual(rotated["remaining"], 2)
        self.assertEqual(rotated["id"], gate["id"])
        self.assertEqual(rotated["expires_at"], gate["expires_at"])
        self.assertNotIn("\n", rotated["join_hint"])
        with self.assertRaises(AuthError) as retired:
            self.enter(self.agent("w2"), gate["code"])
        self.assertEqual(retired.exception.status, 403)
        self.assertIn("无效", str(retired.exception))
        self.assertEqual(self.enter(self.agent("w3"), rotated["code"])["seat"], 2)
        with sqlite3.connect(self.teams.path) as db:
            stored = db.execute("SELECT code_hash FROM team_gates WHERE id=?", (gate["id"],)).fetchone()[0]
        self.assertEqual(stored, hashlib.sha256(rotated["code"].encode()).hexdigest())

    def test_only_owner_can_open_close_rotate_or_read_status(self):
        gate = self.open()
        joined = self.enter(self.agent("w1"), gate["code"])
        worker = self.agent("w1")
        for action in (
            lambda actor: self.teams.open_gate(actor, self.sid, {"role": "worker", "max_joins": 1}),
            lambda actor: self.teams.gate_status(actor, self.sid, "worker"),
            lambda actor: self.teams.close_gate(actor, self.sid, {}),
            lambda actor: self.teams.rotate_gate(actor, self.sid, {}),
        ):
            with self.assertRaises(AuthError) as denied:
                action(worker)
            self.assertEqual(denied.exception.status, 403)
        stranger = self.agent("stranger")
        with self.assertRaises(AuthError) as missing:
            self.teams.gate_status(stranger, self.sid)
        self.assertEqual(missing.exception.status, 404)
        listed = self.teams.gate_status(self.owner, self.sid)["gates"]
        self.assertEqual(listed[0]["id"], gate["id"])
        self.assertNotIn("code", listed[0])
        self.assertEqual(joined["role"], "worker")
        self.assertNotIn(gate["code"], json.dumps(
            self.teams.gate_status(self.owner, self.sid, "worker")))

    def test_one_active_gate_per_role_and_mentor_gate_is_single_join(self):
        self.open(max_joins=2)
        with self.assertRaises(AuthError) as active:
            self.open(max_joins=1)
        self.assertEqual(active.exception.status, 409)
        mentor_gate = self.open(role="mentor", max_joins=1)
        self.assertEqual(
            mentor_gate["join_hint"],
            "diaodu_relay_team_gate_join gate_code=%s role=mentor" % mentor_gate["code"])
        with self.assertRaises(ValueError):
            self.teams.open_gate(self.owner, self.sid, {"role": "mentor", "max_joins": 2})
        with self.assertRaises(AuthError) as environment:
            self.enter(self.agent("env-mentor"), mentor_gate["code"], environment=True)
        self.assertEqual(environment.exception.status, 403)
        self.assertIn("系统环境", str(environment.exception))
        self.assertEqual(self.enter(self.mentor, mentor_gate["code"], environment=False)["seat"], 0)
        with self.assertRaises(AuthError) as spent:
            self.enter(self.agent("other-mentor"), mentor_gate["code"], environment=False)
        self.assertEqual(spent.exception.status, 403)
        self.assertIn("名额已用完", str(spent.exception))
        with self.assertRaises(AuthError):
            self.enter(self.agent("env-mentor"), mentor_gate["code"], environment=True)
        self.teams.close_gate(self.owner, self.sid, {"role": "worker"})
        self.open(max_joins=1)

    def test_worker_must_send_environment_and_single_use_invite_still_works(self):
        gate = self.open(max_joins=2)
        with self.assertRaises(ValueError) as missing:
            self.teams.join_gate(self.agent("bare"), {"gate_code": gate["code"]})
        self.assertIn("environment", str(missing.exception))
        invite = self.teams.invite(self.owner, self.sid, {"role": "worker", "seat": 3})["invite"]
        self.assertGreater(len(invite["code"]), 20)
        seated = self.teams.join(self.agent("pinned"), {
            "code": invite["code"], "environment": dict(ENVIRONMENT)})["session"]
        self.assertEqual(seated["seat"], 3)
        with self.assertRaises(AuthError) as reused:
            self.teams.join(self.agent("again"), {
                "code": invite["code"], "environment": dict(ENVIRONMENT)})
        self.assertEqual(reused.exception.status, 403)
        self.assertIn("已被使用", str(reused.exception))
        with self.assertRaises(AuthError):
            self.enter(self.agent("not-invite"), invite["code"])
        with self.assertRaises(AuthError) as not_gate:
            self.teams.join(self.agent("not-gate"), {
                "code": gate["code"], "environment": dict(ENVIRONMENT)})
        self.assertEqual(not_gate.exception.status, 403)
        self.assertEqual(self.enter(self.agent("via-gate"), gate["code"])["seat"], 1)
        self.assertEqual(self.teams.gate_status(self.owner, self.sid, "worker")["gate"]["remaining"], 1)

    def test_invalid_arguments_and_revoked_identity_do_not_consume_a_seat(self):
        with self.assertRaises(ValueError):
            self.open(max_joins=0)
        with self.assertRaises(ValueError):
            self.open(max_joins=True)
        with self.assertRaises(ValueError):
            self.open(ttl_hours=0)
        with self.assertRaises(ValueError):
            self.open(ttl_hours=169)
        gate = self.open(max_joins=2, ttl_hours=1)
        self.enter(self.agent("w1"), gate["code"])
        self.teams.revoke(self.owner, self.sid, {"role": "worker", "seat": 1})
        with self.assertRaises(AuthError) as blocked:
            self.enter(self.agent("w1"), gate["code"])
        self.assertEqual(blocked.exception.status, 403)
        self.assertIn("撤销", str(blocked.exception))
        self.assertEqual(self.teams.gate_status(self.owner, self.sid, "worker")["gate"]["remaining"], 1)
        self.assertEqual(self.enter(self.agent("w2"), gate["code"])["seat"], 1)
        with self.assertRaises(AuthError):
            self.enter(self.agent("w3"), "OOOOOO")

    def test_join_attempts_are_rate_limited_by_actor_code_and_ip(self):
        gate = self.open(max_joins=3)
        self.teams.GATE_JOIN_ACTOR_LIMIT = 3
        actor = self.agent("scanner")
        for _ in range(3):
            with self.assertRaises(AuthError) as invalid:
                self.enter(actor, "AAAAAA")
            self.assertEqual(invalid.exception.status, 403)
        with self.assertRaises(AuthError) as limited:
            self.enter(actor, "AAAAAA")
        self.assertEqual(limited.exception.status, 429)
        self.assertNotIn("AAAAAA", str(limited.exception))

        self.teams.GATE_JOIN_ACTOR_LIMIT = 12
        self.teams.GATE_JOIN_IP_LIMIT = 2
        for index in range(2):
            with self.assertRaises(AuthError) as invalid:
                self.enter(self.agent("ip%d" % index), "BBBBBB", client_ip="203.0.113.9")
            self.assertEqual(invalid.exception.status, 403)
        with self.assertRaises(AuthError) as limited_ip:
            self.enter(self.agent("ip2"), "BBBBBB", client_ip="203.0.113.9")
        self.assertEqual(limited_ip.exception.status, 429)
        self.assertEqual(self.enter(self.agent("other-net"), gate["code"], client_ip="203.0.113.10")["seat"], 1)

        self.teams.GATE_JOIN_IP_LIMIT = 30
        self.teams.GATE_JOIN_CODE_LIMIT = 2
        self.enter(self.agent("c1"), gate["code"], client_ip="198.51.100.2")
        with self.assertRaises(AuthError) as limited_code:
            self.enter(self.agent("c2"), gate["code"], client_ip="198.51.100.3")
        self.assertEqual(limited_code.exception.status, 429)
        self.assertNotIn(gate["code"], str(limited_code.exception))

    def test_concurrent_gate_joins_do_not_pass_max_joins(self):
        gate = self.open(max_joins=2)
        agents = [self.agent("racer%d" % index) for index in range(5)]
        results = []
        with concurrent.futures.ThreadPoolExecutor(5) as pool:
            futures = [pool.submit(self.enter, agent, gate["code"]) for agent in agents]
            for future in futures:
                try:
                    results.append(future.result()["seat"])
                except AuthError as error:
                    results.append(error.status)
        seats = sorted(item for item in results if item != 403 and item != 429)
        self.assertEqual(seats, [1, 2])
        self.assertEqual(results.count(403), 3)
        status = self.teams.gate_status(self.owner, self.sid, "worker")["gate"]
        self.assertEqual(status["remaining"], 0)
        self.assertEqual(len(status["used_by"]), 2)

    def test_existing_database_gains_gate_tables(self):
        with sqlite3.connect(self.teams.path) as db:
            db.execute("DROP TABLE team_gates")
            db.execute("DROP TABLE team_gate_uses")
        reloaded = RelayTeams(self.root, max_workers=8)
        gate = reloaded.open_gate(self.owner, self.sid, {"role": "worker", "max_joins": 1})["gate"]
        self.assertEqual(len(gate["code"]), 6)
        self.assertEqual(self.enter_on(reloaded, gate["code"])["seat"], 1)

    def enter_on(self, teams, code):
        return teams.join_gate(self.agent("migrated"), {
            "gate_code": code, "environment": dict(ENVIRONMENT)})["session"]


class McpTeamToolSchema(unittest.TestCase):
    """Schema/implementation drift guard for the team MCP tool table.

    mcp.py rejects a tools/call whose arguments miss anything listed in required,
    while relay_teams_mcp.call() reads args["session"] for every tool after the
    create/join/list branches. A tool in that group that omits "session" from its
    schema is therefore impossible to call; the allowlist below is the only
    session-less group.
    """

    SESSIONLESS = {"relay_team_create", "relay_team_join", "relay_team_gate_join", "relay_team_list"}

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
