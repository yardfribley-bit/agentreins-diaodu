import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from diaodu.identity import AuthError
from diaodu.relay import Relay


ENVIRONMENT = {"os_name": "Linux", "os_version": "Ubuntu 24.04", "architecture": "x86_64",
               "shells": ["bash", "sh"], "python_version": "3.12.3"}


class RelayStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.board = self.root / "board.json"
        self.board.write_text('{"untouched":true}')
        self.relay = Relay(self.root)
        self.owner = {"kind": "user", "user_id": "person", "user_name": "管理者", "token": "never-store-owner-token"}
        # Deliberately share user_id: distinct agents must never be merged.
        self.mentor = self.agent("mentor")
        self.worker = self.agent("worker")
        self.other = self.agent("outsider")
        self.created = self.relay.create(self.owner, {"title": "私有协作", "goal": "仅交换指令与结果"})
        self.sid = self.created["session"]["id"]
        self.pairing = {role: self.relay.invite(self.owner, self.sid, {"role": role}) for role in ("mentor", "worker")}

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, name):
        return {"kind": "agent", "agent_id": "ag_" + name, "user_id": "person",
                "agent_name": name, "token": "never-store-agent-token-" + name}

    def join(self, role, actor=None, code=None):
        body = {"code": code or self.pairing[role]["code"]}
        if role == "worker":
            body["environment"] = dict(ENVIRONMENT)
        return self.relay.join(actor or (self.mentor if role == "mentor" else self.worker), body)

    def paired(self):
        self.join("mentor")
        self.join("worker")

    def test_reusable_topic_code_assigns_roles_and_can_be_rotated(self):
        code = self.created["topic_code"]
        mentor = self.relay.join(self.mentor, {"code": code, "role": "mentor"})
        self.assertEqual(mentor["session"]["role"], "mentor")
        worker = self.relay.join(self.worker, {"code": code, "role": "worker", "environment": dict(ENVIRONMENT)})
        self.assertEqual(worker["session"]["role"], "worker")
        self.error(409, lambda: self.relay.join(self.other, {"code": code, "role": "worker", "environment": dict(ENVIRONMENT)}))

        replacement = self.relay.rotate_topic_code(self.owner, self.sid, {})["code"]
        self.assertNotEqual(code, replacement)
        self.error(403, lambda: self.relay.join(self.other, {"code": code, "role": "worker", "environment": dict(ENVIRONMENT)}))

    def test_topic_code_requires_explicit_role_and_worker_environment(self):
        code = self.created["topic_code"]
        with self.assertRaises(ValueError):
            self.relay.join(self.mentor, {"code": code})
        with self.assertRaises(ValueError):
            self.relay.join(self.worker, {"code": code, "role": "worker"})
        self.relay.join(self.mentor, {"code": code, "role": "mentor"})

    def send(self, actor=None, text="消息", kind="note", request_id="one", sid=None, **extra):
        return self.relay.send(actor or self.owner, sid or self.sid,
                               {"text": text, "kind": kind, "request_id": request_id, **extra})

    def error(self, status, call):
        with self.assertRaises(AuthError) as caught:
            call()
        self.assertEqual(caught.exception.status, status)

    def wait_until_waiting(self, count=1):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.relay._condition:
                if len(self.relay._waiters) == count:
                    return
            time.sleep(.005)
        self.fail("long poll did not register")

    def test_private_storage_and_listing_are_isolated_from_public_board(self):
        self.assertEqual(os.stat(self.relay.path).st_mode & 0o777, 0o600)
        self.assertEqual(self.board.read_text(), '{"untouched":true}')
        self.assertEqual(len(self.relay.list_sessions(self.owner)["sessions"]), 1)
        self.assertEqual(self.relay.list_sessions(self.other), {"sessions": []})
        self.error(404, lambda: self.relay.get(self.other, self.sid))
        self.error(404, lambda: self.relay.get(self.other, "rl_missing"))
        self.error(401, lambda: self.relay.list_sessions(None))
        raw = self.relay.path.read_bytes()
        for value in (self.owner["token"], self.pairing["mentor"]["code"],
                      self.pairing["worker"]["code"]):
            self.assertNotIn(value.encode(), raw)
        visible = json.dumps(self.relay.get(self.owner, self.sid))
        self.assertNotIn("pairing", visible)
        self.assertNotIn("token", visible)

    def test_same_user_distinct_agents_have_distinct_membership(self):
        self.paired()
        self.assertEqual(self.relay.get(self.mentor, self.sid)["session"]["role"], "mentor")
        self.assertEqual(self.relay.get(self.worker, self.sid)["session"]["role"], "worker")
        self.error(404, lambda: self.relay.get(self.other, self.sid))
        self.error(404, lambda: self.relay.get({"kind": "user", "user_id": "different"}, self.sid))
        self.assertEqual(self.relay.get(self.owner, self.sid)["session"]["role"], "owner")

    def test_owner_cannot_take_a_slot_and_one_identity_cannot_take_both(self):
        self.error(409, lambda: self.join("mentor", self.owner))
        self.join("mentor")
        self.error(409, lambda: self.join("worker", self.mentor))
        self.join("worker")
        self.error(403, lambda: self.join("mentor", self.other))

    def test_pair_code_single_use_is_atomic_across_store_instances(self):
        another = Relay(self.root)
        barrier = threading.Barrier(2)
        code = self.pairing["worker"]["code"]
        def attempt(store, actor):
            barrier.wait()
            try:
                return store.join(actor, {"code": code, "environment": ENVIRONMENT})["session"]["role"]
            except AuthError as error:
                return error.status
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda args: attempt(*args), [(self.relay, self.worker), (another, self.other)]))
        self.assertEqual(results.count("worker"), 1)
        self.assertEqual(results.count(403), 1)
        self.assertEqual(self.relay.get(self.owner, self.sid)["session"]["last_seq"], 1)

    def test_invite_expiry_rotation_and_revoke_invalidate_old_codes(self):
        old = self.pairing["mentor"]["code"]
        replacement = self.relay.invite(self.owner, self.sid, {"role": "mentor"})
        self.error(403, lambda: self.join("mentor", code=old))
        with sqlite3.connect(self.relay.path) as db:
            db.execute("UPDATE invites SET expires=0 WHERE role='mentor'")
        self.error(403, lambda: self.join("mentor", code=replacement["code"]))
        replacement = self.relay.invite(self.owner, self.sid, {"role": "mentor"})
        self.relay.revoke(self.owner, self.sid, {"role": "mentor"})
        self.error(403, lambda: self.join("mentor", code=replacement["code"]))
        self.assertTrue(replacement["expires_at"].endswith("Z"))

    def test_role_and_identity_fields_cannot_be_spoofed(self):
        self.paired()
        for field in ("role", "sender", "identity", "agent_id", "user_id", "name", "environment"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.send(self.worker, **{field: "owner"})
        self.error(403, lambda: self.send(self.worker, kind="instruction"))
        self.error(403, lambda: self.send(self.mentor, kind="result"))
        self.error(403, lambda: self.send(self.owner, kind="progress"))
        self.error(403, lambda: self.relay.state(self.mentor, self.sid, {"status": "paused"}))
        self.error(403, lambda: self.relay.invite(self.worker, self.sid, {"role": "mentor"}))
        self.error(403, lambda: self.relay.revoke(self.worker, self.sid, {"role": "mentor"}))

    def test_pause_rejects_new_instructions_but_allows_results_and_retry(self):
        self.paired()
        saved = self.send(self.mentor, text="执行任务", kind="instruction")
        self.relay.state(self.owner, self.sid, {"status": "paused"})
        self.error(409, lambda: self.send(self.mentor, kind="instruction", request_id="new"))
        self.error(409, lambda: self.send(self.owner, kind="instruction", request_id="owner-new"))
        repeated = self.send(self.mentor, text="执行任务", kind="instruction")
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(saved["event"], repeated["event"])
        self.send(self.worker, kind="progress", request_id="progress")
        self.send(self.worker, kind="result", request_id="result")
        self.relay.state(self.owner, self.sid, {"status": "active"})
        self.send(self.mentor, kind="instruction", request_id="resumed")

    def test_close_is_terminal_but_preserves_authorized_history(self):
        self.paired()
        original = self.send(self.mentor, kind="instruction")
        closed = self.relay.state(self.owner, self.sid, {"status": "closed"})
        self.assertEqual(closed["session"]["status"], "closed")
        self.assertTrue(self.relay.events(self.worker, self.sid)["events"])
        self.error(409, lambda: self.send(self.worker, request_id="closed"))
        self.error(409, lambda: self.relay.state(self.owner, self.sid, {"status": "active"}))
        self.error(409, lambda: self.relay.invite(self.owner, self.sid, {"role": "worker"}))
        self.error(403, lambda: self.join("worker", self.other))
        self.assertTrue(self.send(self.mentor, kind="instruction")["duplicate"])
        before = time.monotonic()
        result = self.relay.events(self.worker, self.sid, after=closed["session"]["last_seq"], wait=20)
        self.assertEqual(result["events"], [])
        self.assertLess(time.monotonic() - before, .5)
        self.assertEqual(original["event"]["message"]["kind"], "instruction")

    def test_request_id_is_scoped_to_session_and_sender_and_requires_same_content(self):
        self.paired()
        first = self.send(self.owner, text="内容")
        self.assertFalse(first["duplicate"])
        self.assertEqual(self.send(self.owner, text="内容"), {"event": first["event"], "duplicate": True})
        self.error(409, lambda: self.send(self.owner, text="内容 "))
        self.error(409, lambda: self.send(self.owner, text="另一个内容"))
        self.assertFalse(self.send(self.mentor, text="内容")["duplicate"])
        second = self.relay.create(self.owner, {"title": "另一会话", "goal": "隔离"})["session"]["id"]
        self.assertFalse(self.send(self.owner, text="内容", sid=second)["duplicate"])
        with self.assertRaises(ValueError):
            self.send(self.owner, request_id="")

    def test_concurrent_idempotence_and_sequence_allocation(self):
        self.paired()
        stores = [self.relay, Relay(self.root)]
        def same(index):
            return stores[index % 2].send(self.owner, self.sid, {"kind": "note", "text": "一次", "request_id": "concurrent"})
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(same, range(12)))
        self.assertEqual(sum(not row["duplicate"] for row in results), 1)
        self.assertEqual(len({row["event"]["seq"] for row in results}), 1)
        def distinct(index):
            return stores[index % 2].send(self.owner, self.sid, {"kind": "note", "text": str(index), "request_id": "request-" + str(index)})
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(distinct, range(20)))
        self.assertEqual(len({row["event"]["seq"] for row in results}), 20)
        events = self.relay.events(self.owner, self.sid)["events"]
        self.assertEqual([row["seq"] for row in events], list(range(1, len(events) + 1)))

    def test_restart_preserves_membership_messages_ack_and_private_environment(self):
        self.paired()
        sent = self.send(self.mentor, kind="instruction")
        self.relay.ack(self.worker, self.sid, {"seq": sent["event"]["seq"]})
        restarted = Relay(self.root)
        self.assertTrue(restarted.send(self.mentor, self.sid, {"kind": "instruction", "text": "消息", "request_id": "one"})["duplicate"])
        session = restarted.get(self.worker, self.sid)["session"]
        worker = next(peer for peer in session["peers"] if peer["role"] == "worker")
        self.assertEqual(worker["ack_seq"], sent["event"]["seq"])
        self.assertEqual(worker["environment"], ENVIRONMENT)
        self.error(404, lambda: restarted.events(self.other, self.sid))

    def test_ack_is_monotonic_bounded_and_does_not_claim_execution(self):
        self.paired()
        one = self.send(self.mentor, kind="instruction")["event"]["seq"]
        two = self.send(self.worker, kind="progress", request_id="two")["event"]["seq"]
        self.assertEqual(self.relay.ack(self.worker, self.sid, {"seq": two}), {"acknowledged": two})
        self.assertEqual(self.relay.ack(self.worker, self.sid, {"seq": one}), {"acknowledged": two})
        with self.assertRaises(ValueError):
            self.relay.ack(self.worker, self.sid, {"seq": two + 1})
        self.assertEqual(self.relay.get(self.owner, self.sid)["session"]["last_seq"], two)

    def test_replay_pages_have_no_skips_and_reject_cross_session_cursors(self):
        self.paired()
        for index in range(205):
            self.send(request_id=str(index))
        cursor = 0
        replayed = []
        while True:
            page = self.relay.events(self.worker, self.sid, after=cursor)
            self.assertLessEqual(len(page["events"]), 100)
            replayed.extend(row["seq"] for row in page["events"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        self.assertEqual(replayed, list(range(1, 208)))
        self.assertEqual(self.relay.events(self.worker, self.sid, after=cursor)["next_cursor"], cursor)
        with self.assertRaises(ValueError):
            self.relay.events(self.worker, self.sid, after=cursor + 1)
        other_sid = self.relay.create(self.other, {"title": "其他", "goal": "私有"})["session"]["id"]
        self.error(404, lambda: self.relay.events(self.worker, other_sid, after=cursor))

    def test_long_poll_wakes_immediately_after_commit(self):
        self.paired()
        cursor = self.relay.get(self.worker, self.sid)["session"]["last_seq"]
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(self.relay.events, self.worker, self.sid, cursor, 20)
            self.wait_until_waiting()
            started = time.monotonic()
            sent = self.send(self.mentor, kind="instruction")
            result = waiting.result(timeout=1)
        self.assertLess(time.monotonic() - started, .75)
        self.assertEqual(result["events"], [sent["event"]])
        self.assertEqual(len(self.relay._waiters), 0)

    def test_revoke_terminates_already_waiting_reader_and_denies_all_access(self):
        self.paired()
        cursor = self.relay.get(self.worker, self.sid)["session"]["last_seq"]
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(self.relay.events, self.worker, self.sid, cursor, 20)
            self.wait_until_waiting()
            self.relay.revoke(self.owner, self.sid, {"role": "worker"})
            self.error(404, lambda: waiting.result(timeout=1))
        self.assertEqual(self.relay.list_sessions(self.worker), {"sessions": []})
        for call in [lambda: self.relay.get(self.worker, self.sid), lambda: self.send(self.worker),
                     lambda: self.relay.ack(self.worker, self.sid, {"seq": 0}),
                     lambda: self.relay.heartbeat(self.worker, self.sid)]:
            self.error(404, call)
        code = self.relay.invite(self.owner, self.sid, {"role": "worker"})["code"]
        self.join("worker", self.other, code)
        self.error(404, lambda: self.relay.events(self.worker, self.sid))

    def test_revoked_identity_cannot_rejoin_with_topic_code_but_owner_can_reinvite(self):
        code = self.created["topic_code"]
        self.relay.join(self.mentor, {"code": code, "role": "mentor"})
        self.relay.join(self.worker, {"code": code, "role": "worker", "environment": dict(ENVIRONMENT)})
        self.send(self.worker, kind="result", request_id="before-revoke", text="已执行指令")
        self.relay.revoke(self.owner, self.sid, {"role": "worker"})
        # 撤销后不能读历史，也不能继续写。
        self.error(404, lambda: self.relay.get(self.worker, self.sid))
        self.error(404, lambda: self.relay.events(self.worker, self.sid))
        self.error(404, lambda: self.send(self.worker))
        # 原 Topic 码不能重入：同角色、换角色都不行。
        self.error(403, lambda: self.relay.join(
            self.worker, {"code": code, "role": "worker", "environment": dict(ENVIRONMENT)}))
        self.error(403, lambda: self.relay.join(self.worker, {"code": code, "role": "mentor"}))
        # 未撤销的另一端不受影响。
        self.send(self.mentor, kind="instruction", request_id="after-revoke")
        self.assertEqual(self.relay.get(self.mentor, self.sid)["session"]["status"], "active")
        # 管理者用新的一次性配对码可以明确恢复该身份，之后再次撤销仍然拦截。
        fresh = self.relay.invite(self.owner, self.sid, {"role": "worker"})["code"]
        self.join("worker", self.worker, fresh)
        self.assertEqual(self.relay.get(self.worker, self.sid)["session"]["role"], "worker")
        self.relay.revoke(self.owner, self.sid, {"role": "worker"})
        self.error(403, lambda: self.relay.join(
            self.worker, {"code": code, "role": "worker", "environment": dict(ENVIRONMENT)}))

    def test_revoke_tombstone_survives_restart(self):
        self.paired()
        self.relay.revoke(self.owner, self.sid, {"role": "worker"})
        reopened = Relay(self.root)
        self.error(403, lambda: reopened.join(
            self.worker, {"code": self.created["topic_code"], "role": "worker",
                          "environment": dict(ENVIRONMENT)}))

    def test_waiter_limits_release_on_timeout_and_input_is_bounded(self):
        self.paired()
        self.relay.MAX_WAITERS = 1
        cursor = self.relay.get(self.worker, self.sid)["session"]["last_seq"]
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(self.relay.events, self.worker, self.sid, cursor, .2)
            self.wait_until_waiting()
            self.error(429, lambda: self.relay.events(self.worker, self.sid, cursor, 1))
            self.error(429, lambda: self.relay.events(self.mentor, self.sid, cursor, 1))
            self.assertEqual(waiting.result(timeout=1)["events"], [])
        self.assertEqual(len(self.relay._waiters), 0)
        for after, wait in [(-1, 0), (True, 0), (0, -1), (0, 21), (0, float("nan")), (0, float("inf")), (0, True)]:
            with self.subTest(after=after, wait=wait), self.assertRaises(ValueError):
                self.relay.events(self.worker, self.sid, after, wait)

    def test_long_poll_rechecks_membership_before_return_and_each_wait(self):
        self.paired()
        page = self.relay._page
        revoked = False
        def revoked_after_page(*args):
            nonlocal revoked
            result = page(*args)
            if not revoked:
                revoked = True
                self.relay.revoke(self.owner, self.sid, {"role": "worker"})
            return result
        with patch.object(self.relay, "_page", side_effect=revoked_after_page):
            self.error(404, lambda: self.relay.events(self.worker, self.sid))

    def test_worker_environment_is_required_bounded_and_role_protected(self):
        code = self.pairing["worker"]["code"]
        for value in [None, {}, {**ENVIRONMENT, "os_name": ""}, {**ENVIRONMENT, "architecture": "x" * 161},
                      {**ENVIRONMENT, "shells": ["bash"] * 17}, {**ENVIRONMENT, "shells": ["x" * 41]},
                      {**ENVIRONMENT, "python_version": "x" * 81}, {**ENVIRONMENT, "hostname": "must-not-store"}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.relay.join(self.worker, {"code": code, "environment": value})
        mentor_code = self.pairing["mentor"]["code"]
        self.error(403, lambda: self.relay.join(self.mentor, {"code": mentor_code, "environment": ENVIRONMENT}))
        self.paired()
        for actor in (self.owner, self.mentor):
            self.error(403, lambda actor=actor: self.relay.heartbeat(actor, self.sid, {"environment": ENVIRONMENT}))

    def test_environment_heartbeat_changes_are_durable_replayable_and_not_repeated(self):
        self.paired()
        before = self.relay.get(self.worker, self.sid)["session"]["last_seq"]
        self.relay.heartbeat(self.worker, self.sid, {"environment": ENVIRONMENT})
        self.assertEqual(self.relay.get(self.worker, self.sid)["session"]["last_seq"], before)
        changed = {**ENVIRONMENT, "architecture": "aarch64"}
        result = self.relay.heartbeat(self.worker, self.sid, {"environment": changed})
        event = self.relay.events(self.owner, self.sid, after=before)["events"][0]
        self.assertEqual(event["kind"], "environment")
        self.assertEqual(event["environment"], changed)
        self.assertEqual(event["sender"]["role"], "worker")
        self.assertEqual(result["session"]["last_seq"], before + 1)
        self.relay.heartbeat(self.worker, self.sid, {"environment": changed})
        self.assertEqual(self.relay.get(self.owner, self.sid)["session"]["last_seq"], before + 1)

    def test_message_validation_quota_and_close_reservation(self):
        self.paired()
        for kwargs in ({"text": "x" * 8001}, {"text": ""}, {"text": "\x00"}, {"kind": "shell"},
                       {"reply_to": True}, {"reply_to": -1}, {"reply_to": 999}, {"reply_to": 2 ** 100}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.send(**kwargs)
        self.relay.MAX_EVENTS = 5
        self.send(request_id="three")
        self.send(request_id="four")
        self.error(409, lambda: self.send(request_id="over-limit"))
        closed = self.relay.state(self.owner, self.sid, {"status": "closed"})
        self.assertEqual(closed["session"]["last_seq"], 5)
        self.relay.revoke(self.owner, self.sid, {"role": "worker"})
        self.error(404, lambda: self.relay.get(self.worker, self.sid))
        self.assertEqual(self.relay.get(self.owner, self.sid)["session"]["last_seq"], 5)

    def test_open_session_quota_is_transactional_and_close_frees_capacity(self):
        self.relay.MAX_OPEN_SESSIONS = 2
        self.relay.create(self.owner, {"title": "第二个", "goal": "两个"})
        self.error(429, lambda: self.relay.create(self.owner, {"title": "第三个", "goal": "满额"}))
        self.relay.state(self.owner, self.sid, {"status": "closed"})
        self.relay.create(self.owner, {"title": "第三个", "goal": "释放容量"})


if __name__ == "__main__":
    unittest.main()
