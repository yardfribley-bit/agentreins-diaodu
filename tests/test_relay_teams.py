import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

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
        self.code = created["topic_code"]
        self.mentor_session = self.teams.join(self.mentor, {"code": self.code, "role": "mentor"})["session"]
        self.seat1 = self.teams.join(self.worker1, {"code": self.code, "role": "worker",
                                                    "environment": dict(ENVIRONMENT)})["session"]["seat"]
        self.seat2 = self.teams.join(self.worker2, {"code": self.code, "role": "worker",
                                                    "environment": dict(ENVIRONMENT)})["session"]["seat"]

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, name):
        return {"kind": "agent", "agent_id": "ag_" + name, "user_id": "person",
                "agent_name": name, "token": "never-store-" + name}

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

    def test_worker_seat_limit_and_concurrent_joins_do_not_collide(self):
        created = self.teams.create(self.owner, {"title": "并发", "goal": "抢席位"})
        sid, code = created["session"]["id"], created["topic_code"]
        agents = [self.agent(f"racer{i}") for i in range(6)]
        results = []
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            futures = [pool.submit(self.teams.join, agent,
                                   {"code": code, "role": "worker", "environment": dict(ENVIRONMENT)})
                       for agent in agents]
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

    def test_broadcast_instruction_is_visible_to_everyone(self):
        sent = self.send(self.mentor, "instruction", "全体执行", "inst-all", to=None)
        for actor in (self.mentor, self.owner, self.worker1, self.worker2):
            events, _ = self.drain(actor)
            self.assertIn(sent["event"]["seq"], [event["seq"] for event in events])
        sent_all = self.send(self.mentor, "instruction", "全体执行2", "inst-all2", to="all")
        events, _ = self.drain(self.worker1)
        self.assertIn(sent_all["event"]["seq"], [event["seq"] for event in events])

    def test_targeted_instruction_only_reaches_target_worker_and_staff(self):
        sent = self.send(self.mentor, "instruction", "只给二号", "inst-2", to=f"worker:{self.seat2}")
        seq = sent["event"]["seq"]
        self.assertIn(seq, [event["seq"] for event in self.drain(self.worker2)[0]])
        self.assertNotIn(seq, [event["seq"] for event in self.drain(self.worker1)[0]])
        self.assertIn(seq, [event["seq"] for event in self.drain(self.mentor)[0]])
        self.assertIn(seq, [event["seq"] for event in self.drain(self.owner)[0]])

    def test_worker_results_are_private_unless_broadcast(self):
        sent = self.send(self.worker1, "result", "一号结果", "r1")
        seq = sent["event"]["seq"]
        self.assertIn(seq, [event["seq"] for event in self.drain(self.worker1)[0]])
        self.assertIn(seq, [event["seq"] for event in self.drain(self.mentor)[0]])
        self.assertIn(seq, [event["seq"] for event in self.drain(self.owner)[0]])
        self.assertNotIn(seq, [event["seq"] for event in self.drain(self.worker2)[0]])
        broadcast = self.send(self.worker1, "result", "公开结果", "r1-all", to="all")
        self.assertIn(broadcast["event"]["seq"],
                      [event["seq"] for event in self.drain(self.worker2)[0]])
        hidden_progress = self.send(self.worker2, "progress", "私有进度", "p2")
        self.assertNotIn(hidden_progress["event"]["seq"],
                         [event["seq"] for event in self.drain(self.worker1)[0]])

    def test_cursor_advances_over_invisible_events_without_stalling(self):
        targeted = self.send(self.mentor, "instruction", "只给一号", "inst-1", to=f"worker:{self.seat1}")
        visible = self.send(self.mentor, "instruction", "广播", "inst-broadcast")
        events, cursor = self.drain(self.worker2)
        sequences = [event["seq"] for event in events]
        self.assertNotIn(targeted["event"]["seq"], sequences)
        self.assertIn(visible["event"]["seq"], sequences)
        last_seq = self.teams.get(self.worker2, self.sid)["session"]["last_seq"]
        page = self.teams.events(self.worker2, self.sid, after=cursor, wait=0)
        self.assertEqual(page["events"], [])
        self.assertEqual(page["next_cursor"], last_seq)
        again = self.teams.events(self.worker2, self.sid, after=last_seq, wait=0)
        self.assertEqual(again["events"], [])
        self.assertEqual(again["next_cursor"], last_seq)

    def test_result_cannot_reply_to_another_seats_instruction(self):
        targeted = self.send(self.mentor, "instruction", "只给一号", "inst-target",
                             to=f"worker:{self.seat1}")
        with self.assertRaises(AuthError) as caught:
            self.send(self.worker2, "result", "越权回报", "r-over",
                      reply_to=targeted["event"]["seq"])
        self.assertEqual(caught.exception.status, 403)
        broadcast = self.send(self.mentor, "instruction", "广播指令", "inst-broadcast-2")
        allowed = self.send(self.worker2, "result", "正常回报", "r-ok",
                            reply_to=broadcast["event"]["seq"])
        self.assertFalse(allowed["duplicate"])

    def test_instruction_target_must_exist(self):
        with self.assertRaises(ValueError):
            self.send(self.mentor, "instruction", "越界", "inst-bad", to="worker:9")
        with self.assertRaises(ValueError):
            self.send(self.mentor, "instruction", "空席", "inst-empty", to="worker:3")

    def test_request_id_dedupe_covers_target(self):
        first = self.send(self.worker1, "result", "结果", "same-id")
        self.assertTrue(self.send(self.worker1, "result", "结果", "same-id")["duplicate"])
        with self.assertRaises(AuthError):
            self.send(self.worker1, "result", "结果", "same-id", to="all")
        self.assertFalse(first["duplicate"])

    def test_team_storage_is_separate_from_one_to_one_relay(self):
        Relay(self.root)
        with self.teams._read() as db:
            team_tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("team_members", team_tables)
        self.assertNotIn("members", team_tables)
        self.assertNotIn("sessions", team_tables)
        relay = Relay(self.root)
        with relay._read() as db:
            relay_tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("team_members", relay_tables)
        self.assertNotIn("team_events", relay_tables)


if __name__ == "__main__":
    unittest.main()
