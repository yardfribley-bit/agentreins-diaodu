import json
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from diaodu.identity import AuthError, Identity
from diaodu.library import Library
from diaodu.relay_teams import RelayTeams
from diaodu.server import make_handler

ENVIRONMENT = {"os_name": "macOS", "os_version": "test", "architecture": "arm64",
               "shells": ["bash"], "python_version": "3.11.15"}


class WorkSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.teams = RelayTeams(self.root, max_workers=3,
                                public_base_path="/task", public_origin="https://example.test")
        self.owner = self.agent("owner")
        self.mentor = self.agent("mentor")
        self.worker1 = self.agent("worker1")
        self.worker2 = self.agent("worker2")
        created = self.teams.create(self.owner, {"title": "工作面", "goal": "协作"})
        self.sid = created["session"]["id"]
        self.public_id = created["session"]["public_id"]
        self.join_invite(self.mentor, "mentor", environment=False)
        self.seat1 = self.join_invite(self.worker1, "worker")["seat"]
        self.seat2 = self.join_invite(self.worker2, "worker")["seat"]

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, name):
        return {"kind": "agent", "agent_id": "ag_" + name, "user_id": "person",
                "agent_name": name, "token": "never-store-" + name}

    def join_invite(self, actor, role, environment=True):
        code = self.teams.invite(self.owner, self.sid, {"role": role})["invite"]["code"]
        body = {"code": code, "role": role}
        if environment:
            body["environment"] = dict(ENVIRONMENT)
        return self.teams.join(actor, body)["session"]

    def texts(self, actor, stream="work"):
        page = self.teams.events(actor, self.sid, after=0, wait=0, stream=stream)
        found = []
        for event in page["events"]:
            message = event.get("message") or {}
            if message.get("text"):
                found.append(message["text"])
            task = event.get("task") or {}
            if task.get("title"):
                found.append(task["title"])
        return found, page

    def test_claim_board_is_visible_and_instructions_can_reference_it(self):
        first = self.teams.create_task(self.owner, self.sid, {
            "title": "整理来源", "detail": "列出三份材料", "request_id": "task-a"})
        second = self.teams.create_task(self.owner, self.sid, {
            "title": "核对数字", "request_id": "task-b"})
        self.assertEqual(first["task"]["status"], "open")
        self.assertFalse(first["duplicate"])
        again = self.teams.create_task(self.owner, self.sid, {
            "title": "整理来源", "detail": "列出三份材料", "request_id": "task-a"})
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["task"]["id"], first["task"]["id"])
        with self.assertRaises(AuthError):
            self.teams.claim_task(self.owner, self.sid, first["task"]["id"])
        claimed = self.teams.claim_task(self.worker1, self.sid, first["task"]["id"])
        self.assertEqual(claimed["task"]["status"], "in_progress")
        self.assertEqual(claimed["task"]["assignee"]["seat"], self.seat1)
        with self.assertRaises(AuthError) as taken:
            self.teams.claim_task(self.worker2, self.sid, first["task"]["id"])
        self.assertEqual(taken.exception.status, 409)
        other = self.teams.claim_task(self.worker2, self.sid, second["task"]["id"])
        self.assertEqual(other["task"]["assignee"]["seat"], self.seat2)
        moved = self.teams.update_task(self.worker1, self.sid, first["task"]["id"], {"progress": 40})
        self.assertEqual(moved["task"]["progress"], 40)
        done = self.teams.update_task(self.worker1, self.sid, first["task"]["id"], {"status": "done"})
        self.assertEqual(done["task"]["status"], "done")
        self.assertEqual(done["task"]["progress"], 100)
        listed = self.teams.list_tasks(self.worker2, self.sid)
        self.assertEqual({item["title"] for item in listed["tasks"]}, {"整理来源", "核对数字"})
        self.assertEqual(self.teams.list_tasks(self.worker2, self.sid, "done")["tasks"][0]["id"],
                         first["task"]["id"])
        sent = self.teams.send(self.worker1, self.sid, {
            "kind": "result", "text": "来源已整理", "request_id": "result-a",
            "task_id": first["task"]["id"]})
        self.assertEqual(sent["event"]["message"]["task_id"], first["task"]["id"])
        board = self.teams.get(self.worker2, self.sid)["session"]["tasks"]
        self.assertEqual(len(board), 2)
        titles, page = self.texts(self.worker2)
        self.assertIn("整理来源", titles)
        self.assertIn("来源已整理", titles)
        self.assertEqual(page["stream"], "work")
        blob = json.dumps(page, ensure_ascii=False)
        self.assertNotIn("actor_key", blob)
        others = [item for item in page["session"]["workers"] if item["seat"] != self.seat2]
        self.assertTrue(others)
        self.assertTrue(all("environment" not in item for item in others))

    def test_mentor_directs_and_worker_reports_without_election_notes(self):
        appointed = self.teams.appoint(self.owner, self.sid, {"role": "mentor", "seat": 0})
        self.assertEqual(appointed["leader"]["role"], "mentor")
        self.assertEqual(appointed["leader"]["name"], "mentor")
        task = self.teams.create_task(self.mentor, self.sid, {
            "title": "检查端口", "request_id": "lead-task"})["task"]
        instruction = self.teams.send(self.mentor, self.sid, {
            "kind": "instruction", "text": "看 8765", "request_id": "inst-1",
            "to": f"worker:{self.seat1}", "task_id": task["id"]})
        seq = instruction["event"]["seq"]
        self.teams.send(self.worker1, self.sid, {
            "kind": "progress", "text": "正在看", "request_id": "prog-1",
            "reply_to": seq, "task_id": task["id"]})
        result = self.teams.send(self.worker1, self.sid, {
            "kind": "result", "text": "端口开着", "request_id": "res-1",
            "reply_to": seq, "task_id": task["id"]})
        self.teams.send(self.worker1, self.sid, {
            "kind": "note", "text": "HB 1", "request_id": "hb-1"})
        self.teams.send(self.worker2, self.sid, {
            "kind": "note", "text": "ELECT", "request_id": "elect-noise"})
        self.teams.send(self.worker2, self.sid, {
            "kind": "note", "text": "LEADER worker:2", "request_id": "leader-noise"})
        titles, page = self.texts(self.worker2)
        self.assertIn("看 8765", titles)
        self.assertIn("端口开着", titles)
        self.assertNotIn("HB 1", titles)
        self.assertNotIn("ELECT", titles)
        self.assertNotIn("LEADER worker:2", titles)
        self.assertEqual(result["event"]["message"]["reply_to"], seq)
        leader = self.teams.who_is_leader(self.worker1, self.sid)
        self.assertEqual(leader["leader"]["seat"], 0)
        self.assertEqual(leader["leader"]["role"], "mentor")
        self.assertNotIn("HB", json.dumps(leader))
        online = self.teams.list_online(self.worker2, self.sid)
        self.assertIn("worker1", [item["name"] for item in online["online"]])
        self.assertNotIn("environment", json.dumps(online))
        renewed = self.teams.leader_heartbeat(self.mentor, self.sid)
        self.assertEqual(renewed["leader"]["role"], "mentor")
        with self.assertRaises(AuthError):
            self.teams.leader_heartbeat(self.worker1, self.sid)

    def test_election_has_one_leader_and_protocol_stays_off_the_work_stream(self):
        first = self.teams.campaign(self.worker1, self.sid)
        self.assertTrue(first["election"]["open"])
        self.assertIsNone(first["election"]["leader"])
        second = self.teams.campaign(self.worker2, self.sid)
        self.assertIsNone(second["election"]["leader"])
        self.assertEqual(second["election"]["term"], first["election"]["term"])
        elected = self.teams.vote(self.mentor, self.sid, {
            "role": "worker", "seat": self.seat1, "term": first["election"]["term"]})
        self.assertEqual(elected["election"]["leader"]["seat"], self.seat1)
        self.assertFalse(elected["election"]["open"])
        with self.assertRaises(AuthError) as blocked:
            self.teams.campaign(self.worker2, self.sid)
        self.assertEqual(blocked.exception.status, 409)
        stolen = self.teams.send(self.worker2, self.sid, {
            "kind": "note", "text": "LEADER", "request_id": "steal"})
        self.assertFalse(stolen["applied"])
        self.assertEqual(stolen["reason"], "not_leader")
        self.assertEqual(stolen["leader"]["seat"], self.seat1)
        noise = self.teams.send(self.worker2, self.sid, {
            "kind": "note", "text": "ELECT", "request_id": "late-elect"})
        self.assertEqual(noise["reason"], "leader_active")
        heartbeat = self.teams.send(self.worker1, self.sid, {
            "kind": "note", "text": "HB", "request_id": "hb-leader"})
        self.assertTrue(heartbeat["applied"])
        self.assertNotIn("event", heartbeat)
        duplicate = self.teams.send(self.worker1, self.sid, {
            "kind": "note", "text": "HB", "request_id": "hb-leader"})
        self.assertTrue(duplicate["duplicate"])
        work_texts, work = self.texts(self.worker2)
        self.assertFalse(any(text.startswith(("HB", "ELECT", "LEADER", "VOTE")) for text in work_texts))
        self.assertEqual(work["session"]["leader"]["seat"], self.seat1)
        system = self.teams.events(self.worker2, self.sid, after=0, wait=0, stream="system")
        ops = [event["op"] for event in system["events"]]
        self.assertIn("elected", ops)
        self.assertNotIn("HB", ops)
        self.assertTrue(all(event["kind"] == "protocol" for event in system["events"]))
        directed = self.teams.send(self.worker1, self.sid, {
            "kind": "instruction", "text": "你做核对", "request_id": "from-leader",
            "to": f"worker:{self.seat2}"})
        self.assertEqual(directed["event"]["sender"]["seat"], self.seat1)
        parent = self.teams.create_task(self.worker1, self.sid, {
            "title": "总任务", "request_id": "parent"})["task"]
        child = self.teams.create_task(self.worker1, self.sid, {
            "title": "步骤甲", "request_id": "child", "parent_id": parent["id"]})["task"]
        assigned = self.teams.assign_task(self.worker1, self.sid, child["id"],
                                           {"role": "worker", "seat": self.seat2})
        self.assertEqual(assigned["task"]["assignee"]["seat"], self.seat2)
        self.teams.update_task(self.worker2, self.sid, child["id"], {"status": "done", "progress": 100})
        merged = self.teams.update_task(self.worker1, self.sid, parent["id"], {"status": "done"})
        self.assertEqual(merged["task"]["status"], "done")
        self.teams.send(self.worker1, self.sid, {
            "kind": "result", "text": "两步已合并", "request_id": "merge", "task_id": parent["id"]})
        titles, _page = self.texts(self.worker2)
        self.assertIn("步骤甲", titles)
        self.assertIn("两步已合并", titles)

    def test_expired_lease_allows_a_new_term_but_not_two_leaders(self):
        self.teams.appoint(self.owner, self.sid, {"role": "worker", "seat": self.seat1})
        with sqlite3.connect(self.teams.path) as db:
            db.execute("UPDATE team_sessions SET leader_lease=? WHERE id=?", (0, self.sid))
        opened = self.teams.campaign(self.worker2, self.sid)
        self.assertIsNone(opened["election"]["leader"])
        self.assertGreater(opened["election"]["term"], 1)
        finished = self.teams.vote(self.worker1, self.sid, {"role": "worker", "seat": self.seat2})
        self.assertEqual(finished["election"]["leader"]["seat"], self.seat2)
        self.assertEqual(self.teams.who_is_leader(self.owner, self.sid)["leader"]["name"], "worker2")

        def campaign(actor, box):
            try:
                box.append(self.teams.campaign(actor, self.sid)["election"]["leader"])
            except AuthError as error:
                box.append(error.status)

        with sqlite3.connect(self.teams.path) as db:
            db.execute("UPDATE team_sessions SET leader_lease=? WHERE id=?", (0, self.sid))
        left, right = [], []
        threads = [threading.Thread(target=campaign, args=(self.worker1, left)),
                   threading.Thread(target=campaign, args=(self.worker2, right))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        view = self.teams.who_is_leader(self.mentor, self.sid)
        leaders = [item for item in (left + right) if isinstance(item, dict)]
        seats = {item["seat"] for item in leaders}
        if view["leader"] is None:
            self.assertEqual(seats, set())
        else:
            self.assertEqual(seats, {view["leader"]["seat"]})
        self.assertLessEqual(len(seats), 1)

    def test_public_directory_shows_work_and_hides_protocol_env_and_gate_code(self):
        gate = self.teams.open_gate(self.owner, self.sid, {"role": "worker", "max_joins": 1})
        code = gate["gate"]["code"]
        self.teams.join_gate(self.agent("gated"), {
            "gate_code": code, "environment": dict(ENVIRONMENT)})
        self.teams.create_task(self.owner, self.sid, {"title": "公开步骤", "request_id": "pub-task"})
        self.teams.send(self.mentor, self.sid, {
            "kind": "instruction", "text": "把结果写下来", "request_id": "pub-inst"})
        self.teams.send(self.worker1, self.sid, {
            "kind": "note", "text": "HB", "request_id": "pub-hb"})
        self.teams.send(self.worker1, self.sid, {
            "kind": "note", "text": "ELECT", "request_id": "pub-elect"})
        changed = dict(ENVIRONMENT, python_version="3.12.9")
        self.teams.heartbeat(self.worker1, self.sid, {"environment": changed})
        detail = self.teams.directory_conversation(self.public_id)
        self.assertEqual(detail["tasks"][0]["title"], "公开步骤")
        self.assertIsNone(detail["leader"])
        events = self.teams.directory_events(self.public_id, 0, 50)
        blob = json.dumps({"detail": detail, "events": events}, ensure_ascii=False)
        self.assertIn("公开步骤", blob)
        self.assertIn("把结果写下来", blob)
        self.assertNotIn(code, blob)
        self.assertNotIn("ELECT", blob)
        self.assertNotIn("macOS", blob)
        self.assertNotIn("3.12.9", blob)
        self.assertNotIn("actor_key", blob)
        self.assertNotIn("environment", [event["kind"] for event in events["events"]])
        self.assertNotIn("protocol", [event["kind"] for event in events["events"]])

    def _age(self, role, seat, last_seen):
        with sqlite3.connect(self.teams.path) as db:
            db.execute("UPDATE team_members SET last_seen=? WHERE session_id=? AND role=? AND seat=?",
                       (last_seen, self.sid, role, seat))

    def test_dead_leader_is_dropped_from_quorum_and_a_new_leader_is_elected(self):
        seat3 = self.join_invite(self.agent("worker3"), "worker")["seat"]
        self.teams.campaign(self.worker1, self.sid)
        self.teams.vote(self.worker2, self.sid, {"role": "worker", "seat": self.seat1})
        seated = self.teams.vote(self.mentor, self.sid, {"role": "worker", "seat": self.seat1})
        self.assertEqual(seated["elected"]["seat"], self.seat1)
        self.assertFalse(seated["already_voted"])
        held = self.teams.who_is_leader(self.owner, self.sid)
        self.assertGreaterEqual(held["votes"], held["needed"])
        self.assertFalse(held["open"])
        eligible_while_leading = held["eligible"]
        self._age("worker", seat3, time.time() - 100)
        # A seated worker going quiet must not change the tally of a live lease.
        still = self.teams.who_is_leader(self.owner, self.sid)
        self.assertEqual(still["eligible"], eligible_while_leading)
        self.assertGreaterEqual(still["votes"], still["needed"])
        self._age("worker", seat3, time.time())
        now = time.time()
        with sqlite3.connect(self.teams.path) as db:
            db.execute("UPDATE team_sessions SET leader_lease=? WHERE id=?", (now - 1, self.sid))
        # Last seen 5s ago: inside the 60s roster window and the 20s lease window,
        # but the lease itself has ended, so this seat is not online and not a voter.
        self._age("worker", self.seat1, now - 5)
        self._age("mentor", 0, now - 30)
        roster = self.teams.list_online(self.worker2, self.sid)
        dead = next(item for item in roster["members"] if item["role"] == "worker" and item["seat"] == self.seat1)
        mentor = next(item for item in roster["members"] if item["role"] == "mentor")
        self.assertFalse(dead["online"])
        self.assertFalse(dead["live"])
        self.assertTrue(mentor["online"])
        self.assertFalse(mentor["live"])
        self.assertGreaterEqual(mentor["last_seen_age_s"], 20)
        opened = self.teams.campaign(self.worker2, self.sid)
        self.assertEqual(opened["election"]["term"], held["term"] + 1)
        self.assertEqual(opened["election"]["eligible"], 2)
        self.assertIsNone(opened["elected"])
        duplicate = self.teams.vote(self.worker2, self.sid, {"role": "worker", "seat": self.seat2})
        self.assertTrue(duplicate["already_voted"])
        self.assertIsNone(duplicate["elected"])
        finished = self.teams.vote(self.agent("worker3"), self.sid, {"role": "worker", "seat": self.seat2})
        self.assertEqual(finished["elected"]["seat"], self.seat2)
        self.assertEqual(finished["election"]["leader"]["name"], "worker2")
        self.assertGreaterEqual(finished["election"]["votes"], finished["election"]["needed"])
        self.assertFalse(finished["election"]["open"])
        system = self.teams.events(self.worker2, self.sid, after=0, wait=0, stream="system")
        elected_terms = [event["term"] for event in system["events"] if event["op"] == "elected"]
        self.assertEqual(elected_terms, [held["term"], held["term"] + 1])

    def test_lone_survivor_becomes_leader_without_the_offline_majority(self):
        self.teams.appoint(self.owner, self.sid, {"role": "worker", "seat": self.seat1})
        now = time.time()
        with sqlite3.connect(self.teams.path) as db:
            db.execute("UPDATE team_sessions SET leader_lease=? WHERE id=?", (now - 1, self.sid))
        self._age("worker", self.seat1, now - 5)
        self._age("mentor", 0, now - 100)
        self._age("worker", self.seat2, now - 100)
        elected = self.teams.campaign(self.worker2, self.sid)
        self.assertEqual(elected["elected"]["seat"], self.seat2)
        self.assertEqual(self.teams.who_is_leader(self.owner, self.sid)["leader"]["seat"], self.seat2)

    def test_stalled_election_backs_off_instead_of_opening_terms(self):
        opened = self.teams.campaign(self.worker1, self.sid)
        term = opened["election"]["term"]
        self.assertTrue(opened["election"]["open"])
        with sqlite3.connect(self.teams.path) as db:
            db.execute("UPDATE team_sessions SET election_until=? WHERE id=?", (time.time() - 1, self.sid))
        before = self.teams.events(self.mentor, self.sid, after=0, wait=0, stream="system")["events"]
        for _ in range(4):
            stalled = self.teams.campaign(self.worker1, self.sid)
            self.assertGreaterEqual(stalled["retry_after"], 1)
            self.assertEqual(stalled["election"]["term"], term)
            self.assertIsNone(stalled["elected"])
        after = self.teams.events(self.mentor, self.sid, after=0, wait=0, stream="system")["events"]
        self.assertEqual([event["op"] for event in after], [event["op"] for event in before])
        with sqlite3.connect(self.teams.path) as db:
            db.execute("UPDATE team_sessions SET election_backoff_until=? WHERE id=?", (time.time() - 1, self.sid))
        resumed = self.teams.campaign(self.worker2, self.sid)
        self.assertEqual(resumed["election"]["term"], term + 1)
        self.assertNotIn("retry_after", resumed)

    def test_progress_ticks_stay_off_the_work_stream(self):
        task = self.teams.create_task(self.owner, self.sid, {
            "title": "报进度", "request_id": "prog-task"})["task"]
        self.teams.claim_task(self.worker1, self.sid, task["id"])
        before = [event for event in self.teams.events(self.worker2, self.sid, after=0, wait=0)["events"]
                  if event["kind"] == "task"]
        for progress in (10, 40, 70):
            moved = self.teams.update_task(self.worker1, self.sid, task["id"], {"progress": progress})
            self.assertTrue(moved["coalesced"])
            self.assertEqual(moved["task"]["progress"], progress)
        middle = [event for event in self.teams.events(self.worker2, self.sid, after=0, wait=0)["events"]
                  if event["kind"] == "task"]
        self.assertEqual(len(middle), len(before))
        self.assertNotIn("updated", [event.get("action") for event in middle])
        done = self.teams.update_task(self.worker1, self.sid, task["id"], {"status": "done"})
        self.assertFalse(done["coalesced"])
        after = [event for event in self.teams.events(self.worker2, self.sid, after=0, wait=0)["events"]
                 if event["kind"] == "task"]
        self.assertEqual([event.get("action") for event in after], ["created", "claimed", "updated"])
        board = self.teams.list_tasks(self.owner, self.sid)["tasks"]
        self.assertEqual(board[0]["progress"], 100)

    def test_events_can_omit_the_session_view(self):
        self.teams.create_task(self.owner, self.sid, {"title": "视图", "request_id": "view-task"})
        full = self.teams.events(self.worker1, self.sid, after=0, wait=0, view=1)
        slim = self.teams.events(self.worker1, self.sid, after=0, wait=0, view=0)
        self.assertIn("tasks", full["session"])
        self.assertIn("members", full["session"])
        self.assertNotIn("tasks", slim["session"])
        self.assertNotIn("members", slim["session"])
        self.assertNotIn("workers", slim["session"])
        self.assertEqual(slim["session"]["last_seq"], full["session"]["last_seq"])
        self.assertEqual(slim["session"]["role"], "worker")
        titles = [event.get("task", {}).get("title") for event in slim["events"]]
        self.assertIn("视图", titles)
        self.assertNotIn("environment", json.dumps(slim["session"]))
        with self.assertRaises(ValueError):
            self.teams.events(self.worker1, self.sid, after=0, wait=0, view=2)

    def test_public_page_lists_leader_terms_without_secrets(self):
        appointed = self.teams.appoint(self.owner, self.sid, {"role": "worker", "seat": self.seat1})
        self.assertEqual(appointed["elected"]["seat"], self.seat1)
        detail = self.teams.directory_conversation(self.public_id)
        self.assertEqual(detail["leader"]["seat"], self.seat1)
        self.assertEqual(detail["leader"]["name"], "worker1")
        self.assertGreaterEqual(detail["leader"]["term"], 1)
        self.assertEqual(detail["leader_terms"][-1]["seat"], self.seat1)
        self.assertEqual(detail["leader_terms"][-1]["term"], detail["leader"]["term"])
        blob = json.dumps(detail, ensure_ascii=False)
        self.assertNotIn("actor_key", blob)
        self.assertNotIn("leader_key", blob)
        self.assertNotIn("environment", blob)
        script = Path(__file__).resolve().parent.parent.joinpath("static", "conversations.js").read_text()
        self.assertIn("badge leader", script)
        self.assertIn("leader_terms", script)
        self.assertNotIn("innerHTML", script)

    def test_read_only_member_stays_in_the_electorate(self):
        seat3 = self.join_invite(self.agent("worker3"), "worker")["seat"]
        # Inside the refresh interval: a read must not rewrite last_seen.
        self._age("mentor", 0, time.time() - 2)
        with sqlite3.connect(self.teams.path) as db:
            held = db.execute("SELECT last_seen FROM team_members WHERE session_id=? AND role='mentor'",
                              (self.sid,)).fetchone()[0]
        self.teams.events(self.mentor, self.sid, after=0, wait=0)
        with sqlite3.connect(self.teams.path) as db:
            skipped = db.execute("SELECT last_seen FROM team_members WHERE session_id=? AND role='mentor'",
                                 (self.sid,)).fetchone()[0]
        self.assertEqual(skipped, held)
        # Outside the lease, still inside the old 60s touch gap. A read counts.
        self._age("mentor", 0, time.time() - 25)
        self.teams.who_is_leader(self.mentor, self.sid)
        self.teams.list_tasks(self.mentor, self.sid)
        self.teams.events(self.mentor, self.sid, after=0, wait=0)
        with sqlite3.connect(self.teams.path) as db:
            refreshed = db.execute("SELECT last_seen FROM team_members WHERE session_id=? AND role='mentor'",
                                   (self.sid,)).fetchone()[0]
        self.assertLess(time.time() - refreshed, self.teams.PRESENCE_REFRESH)
        opened = self.teams.campaign(self.worker1, self.sid)
        self.assertEqual(opened["election"]["eligible"], 4)
        self.assertEqual(opened["election"]["needed"], 3)
        self.assertIsNone(opened["elected"])
        # The reader then goes quiet for the rest of this term. needed stays put,
        # so two votes cannot elect just because the mentor blipped out.
        self._age("mentor", 0, time.time() - 25)
        voted = self.teams.vote(self.worker2, self.sid, {"role": "worker", "seat": self.seat1})
        self.assertIsNone(voted["elected"])
        self.assertEqual(voted["election"]["eligible"], 4)
        self.assertEqual(voted["election"]["needed"], 3)
        self.assertEqual(voted["election"]["votes"], 2)
        quiet = self.teams.list_online(self.worker1, self.sid)
        mentor = next(item for item in quiet["members"] if item["role"] == "mentor")
        self.assertTrue(mentor["online"])
        self.assertFalse(mentor["live"])
        self.assertNotIn("mentor", [item["name"] for item in quiet["live"]])
        self.assertIn(seat3, [item["seat"] for item in quiet["live"] if item["role"] == "worker"])

    def test_dead_worker_is_not_live_inside_the_online_window(self):
        self._age("worker", self.seat2, time.time() - 25)
        roster = self.teams.list_online(self.worker1, self.sid)
        worker = next(item for item in roster["members"]
                      if item["role"] == "worker" and item["seat"] == self.seat2)
        self.assertTrue(worker["online"])
        self.assertFalse(worker["live"])
        self.assertGreaterEqual(worker["last_seen_age_s"], 25)
        self.assertLess(worker["last_seen_age_s"], self.teams.ONLINE_SECONDS)
        self.assertNotIn(self.seat2, [item["seat"] for item in roster["live"] if item["role"] == "worker"])
        session = self.teams.get(self.owner, self.sid)["session"]
        member = next(item for item in session["members"]
                      if item["role"] == "worker" and item["seat"] == self.seat2)
        self.assertTrue(member["online"])
        self.assertFalse(member["live"])
        self.assertGreaterEqual(member["last_seen_age_s"], 25)
        peer = next(item for item in session["workers"] if item["seat"] == self.seat2)
        self.assertFalse(peer["live"])

    def test_assign_rejects_a_seat_outside_the_lease(self):
        self._age("worker", self.seat2, time.time() - 25)
        task = self.teams.create_task(self.owner, self.sid, {
            "title": "交给还在的人", "request_id": "assign-live"})["task"]
        with self.assertRaises(AuthError) as caught:
            self.teams.assign_task(self.owner, self.sid, task["id"],
                                   {"role": "worker", "seat": self.seat2})
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("allow_offline", str(caught.exception))
        board = self.teams.list_tasks(self.owner, self.sid)["tasks"]
        self.assertEqual(board[0]["status"], "open")
        self.assertIsNone(board[0]["assignee"])
        with self.assertRaises(ValueError):
            self.teams.assign_task(self.owner, self.sid, task["id"],
                                   {"role": "worker", "seat": self.seat2, "allow_offline": "yes"})
        forced = self.teams.assign_task(self.owner, self.sid, task["id"],
                                        {"role": "worker", "seat": self.seat2, "allow_offline": True})
        self.assertEqual(forced["task"]["assignee"]["seat"], self.seat2)
        self.assertIn("租约", forced["warning"])
        other = self.teams.create_task(self.owner, self.sid, {
            "title": "交给在席的人", "request_id": "assign-live-ok"})["task"]
        clean = self.teams.assign_task(self.owner, self.sid, other["id"],
                                       {"role": "worker", "seat": self.seat1})
        self.assertEqual(clean["task"]["assignee"]["seat"], self.seat1)
        self.assertNotIn("warning", clean)

    def test_owner_read_of_leader_or_tasks_counts_as_online(self):
        with sqlite3.connect(self.teams.path) as db:
            db.execute("UPDATE team_members SET last_seen=0 WHERE session_id=? AND role='owner'", (self.sid,))
        quiet = self.teams.list_online(self.worker1, self.sid)
        self.assertNotIn("owner", [item["name"] for item in quiet["online"]])
        self.teams.who_is_leader(self.owner, self.sid)
        seen = self.teams.list_online(self.worker1, self.sid)
        self.assertIn("owner", [item["name"] for item in seen["online"]])
        with sqlite3.connect(self.teams.path) as db:
            db.execute("UPDATE team_members SET last_seen=0 WHERE session_id=? AND role='owner'", (self.sid,))
        self.teams.list_tasks(self.owner, self.sid)
        seen = self.teams.list_online(self.worker1, self.sid)
        self.assertIn("owner", [item["name"] for item in seen["online"]])

    def test_old_team_database_gains_work_columns(self):
        fresh = Path(self.temporary.name) / "legacy"
        fresh.mkdir()
        path = fresh / "relay_teams.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE team_sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL,
                status TEXT NOT NULL, owner_key TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL,
                last_seq INTEGER NOT NULL DEFAULT 0,
                public_read INTEGER NOT NULL DEFAULT 0, public_token_hash TEXT,
                visibility TEXT NOT NULL DEFAULT 'public', public_id TEXT)""")
        reloaded = RelayTeams(fresh, max_workers=3)
        with sqlite3.connect(reloaded.path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(team_sessions)")}
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("leader_term", columns)
        self.assertIn("protocol_seq", columns)
        self.assertIn("election_backoff_until", columns)
        self.assertIn("election_needed", columns)
        self.assertIn("team_tasks", tables)
        self.assertIn("team_protocol", tables)
        created = reloaded.create(self.owner, {"title": "迁移", "goal": "仍可建队"})
        self.assertIsNone(created["session"]["leader"])
        self.assertEqual(created["session"]["tasks"], [])


class WorkSurfaceHTTP(unittest.TestCase):
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
        self.actors = {name: self.identity.connect(name, "测试Agent") for name in ("owner", "w1", "w2")}
        status, created = self.req("POST", "/v1/relay/teams", {"title": "HTTP工作面", "goal": "同一份数据"}, "owner")
        self.assertEqual(status, 201)
        self.sid = created["session"]["id"]
        self.public_id = created["session"]["public_id"]
        status, gate = self.req("POST", f"/v1/relay/teams/{self.sid}/gate",
                                {"role": "worker", "max_joins": 2}, "owner")
        self.assertEqual(status, 201)
        self.code = gate["gate"]["code"]
        for name in ("w1", "w2"):
            status, joined = self.req("POST", "/v1/relay/teams/gate/join",
                                      {"gate_code": self.code, "environment": dict(ENVIRONMENT)}, name)
            self.assertEqual(status, 200, joined)
        self.seat = {"w1": 1, "w2": 2}

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temporary.cleanup()

    def req(self, method, path, body=None, role=None):
        headers = {"Content-Type": "application/json"}
        if role:
            headers["Authorization"] = "Bearer " + self.actors[role]["token"]
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urlopen(Request(self.base + path, data=data, headers=headers, method=method), timeout=30) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def mcp(self, role, name, arguments):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "diaodu_" + name, "arguments": arguments}}
        status, body = self.req("POST", "/v1/mcp", payload, role)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["result"]["isError"], body)
        return json.loads(body["result"]["content"][0]["text"])

    def test_rest_and_mcp_cover_tasks_leader_and_filtered_reads(self):
        status, tools = self.req("POST", "/v1/mcp", {
            "jsonrpc": "2.0", "id": 0, "method": "tools/list"}, "owner")
        self.assertEqual(status, 200)
        names = {item["name"] for item in tools["result"]["tools"]}
        for name in ("diaodu_relay_team_task_create", "diaodu_relay_team_who_is_leader",
                     "diaodu_relay_team_list_online", "diaodu_relay_team_elect",
                     "diaodu_relay_team_read"):
            self.assertIn(name, names)
        created = self.mcp("owner", "relay_team_task_create", {
            "session": self.sid, "title": "MCP任务", "request_id": "mcp-task"})
        task_id = created["task"]["id"]
        status, listed = self.req("GET", f"/v1/relay/teams/{self.sid}/tasks?status=open", role="w1")
        self.assertEqual(status, 200)
        self.assertEqual(listed["tasks"][0]["id"], task_id)
        status, claimed = self.req("POST", f"/v1/relay/teams/{self.sid}/tasks/{task_id}/claim", {}, "w1")
        self.assertEqual(status, 200)
        self.assertEqual(claimed["task"]["status"], "in_progress")
        status, assigned = self.req("POST", f"/v1/relay/teams/{self.sid}/tasks/{task_id}/assign",
                                    {"role": "worker", "seat": self.seat["w2"]}, "owner")
        self.assertEqual(status, 200)
        self.assertEqual(assigned["task"]["assignee"]["seat"], 2)
        status, updated = self.req("POST", f"/v1/relay/teams/{self.sid}/tasks/{task_id}/update",
                                   {"progress": 25}, "w2")
        self.assertEqual(status, 200)
        self.assertEqual(updated["task"]["progress"], 25)
        status, leader = self.req("POST", f"/v1/relay/teams/{self.sid}/leader",
                                  {"role": "worker", "seat": 1}, "owner")
        self.assertEqual(status, 200)
        self.assertEqual(leader["leader"]["name"], "w1")
        status, who = self.req("GET", f"/v1/relay/teams/{self.sid}/leader", role="w2")
        self.assertEqual(who["leader"]["seat"], 1)
        status, online = self.req("GET", f"/v1/relay/teams/{self.sid}/online", role="w1")
        self.assertIn("w2", [item["name"] for item in online["online"]])
        self.assertNotIn("environment", json.dumps(online))
        self.mcp("w1", "relay_team_send", {
            "session": self.sid, "kind": "instruction", "text": "继续核对",
            "request_id": "mcp-inst", "task_id": task_id, "to": "worker:2"})
        self.mcp("w2", "relay_team_send", {
            "session": self.sid, "kind": "note", "text": "HB", "request_id": "mcp-hb"})
        self.mcp("w2", "relay_team_send", {
            "session": self.sid, "kind": "note", "text": "ELECT", "request_id": "mcp-elect"})
        page = self.mcp("w1", "relay_team_read", {"session": self.sid, "after": 0, "wait": 0})
        blob = json.dumps(page, ensure_ascii=False)
        self.assertIn("继续核对", blob)
        self.assertIn("MCP任务", blob)
        self.assertNotIn("\"text\": \"HB\"", blob)
        self.assertNotIn("\"text\": \"ELECT\"", blob)
        self.assertEqual(page["session"]["leader"]["seat"], 1)
        self.assertNotIn(self.code, blob)
        status, slim = self.req("GET", f"/v1/relay/teams/{self.sid}/events?after=0&wait=0&view=0", role="w1")
        self.assertEqual(status, 200)
        self.assertNotIn("tasks", slim["session"])
        self.assertNotIn("members", slim["session"])
        self.assertEqual(slim["session"]["last_seq"], page["session"]["last_seq"])
        self.assertNotIn(self.code, json.dumps(slim))
        self.assertEqual(self.req("GET", f"/v1/relay/teams/{self.sid}/events?view=2", role="w1")[0], 400)
        status, public = self.req("GET", f"/v1/relay/public/conversations/{self.public_id}/events?after=0&limit=50")
        self.assertEqual(status, 200)
        public_blob = json.dumps(public, ensure_ascii=False)
        self.assertIn("继续核对", public_blob)
        self.assertNotIn(self.code, public_blob)
        self.assertNotIn("ELECT", public_blob)
        self.assertNotIn("macOS", public_blob)


if __name__ == "__main__":
    unittest.main()
