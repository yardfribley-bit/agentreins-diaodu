"""One-to-many relay teams.

Deliberately separate from the one-to-one relay: its own SQLite file, tables,
routes (added in a later batch) and tool group. Nothing here touches
relay.sqlite3 or the existing `/v1/relay/sessions/...` code.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from diaodu import relay_files
from diaodu.identity import AuthError
from diaodu.relay import (
    DEFAULT_MAX_WAITERS, DEFAULT_SESSION_WAITERS, FILE_ID_RE, PUBLIC_ID_RE, Relay,
    _actor, _body, _claim_waiter, _environment, _iso, _long_poll_ready, _mint_public_id,
    _name, _preview, _resolve_waiter_limits, _text, directory_rows, ensure_directory_columns,
    protocol_op,
)


class RelayTeams:
    MAX_WORKERS_CAP = 8
    MAX_EVENTS = 5000
    PAGE_SIZE = 100
    # Same ceiling as Topic. See diaodu.relay.DEFAULT_MAX_WAITERS: the old 32
    # was one set for every team session, so one busy team starved the rest.
    MAX_WAITERS = DEFAULT_MAX_WAITERS
    MAX_SESSION_WAITERS = DEFAULT_SESSION_WAITERS
    ONLINE_SECONDS = 60
    # Authenticated reads rewrite last_seen at this interval. It stays well
    # under LEASE_SECONDS so a coordinator that only polls or long-polls does
    # not fall out of the quorum between heartbeats. ONLINE_SECONDS is only
    # the slower roster flag and is not used for elections or assignment.
    PRESENCE_REFRESH = 5
    # Seat numbers are never recycled. max_workers caps how many workers sit
    # at once; the number space is larger so a revoked seat stays unambiguous
    # in to=worker:N history.
    SEAT_CAP = 10000
    # A worker gate starts at most max_workers joins. expand_gate can raise
    # that without rotating the code, up to this cap, so a revoked seat can
    # be replaced. Concurrent seats still stop at max_workers.
    GATE_JOIN_CAP = 64
    SIGNED_SECONDS = relay_files.SIGNED_SECONDS
    INVITE_SECONDS = 24 * 3600
    # Short join gates are weaker than 24-byte invite tokens. 6 chars from a
    # 31-symbol alphabet (no 0/O/1/I/L) is the top of the requested 4–6 range.
    # Plaintext codes are returned once to the owner and stored only as sha256.
    GATE_SECONDS = 24 * 3600
    GATE_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    GATE_CODE_LENGTH = 6
    GATE_TTL_MIN_HOURS = 1
    GATE_TTL_MAX_HOURS = 168
    GATE_JOIN_WINDOW = 60.0
    GATE_JOIN_CODE_LIMIT = 30
    GATE_JOIN_ACTOR_LIMIT = 12
    GATE_JOIN_IP_LIMIT = 30
    MAX_FILE_BYTES = 100 * 1024 * 1024
    MAX_ATTACHMENTS = 20
    # Raft-lite lease. A live leader blocks a new term so two workers cannot
    # both observe themselves as leader. Heartbeats renew the lease; they are
    # not work events. Election transitions go to team_protocol.
    LEASE_SECONDS = 20
    ELECTION_WINDOW = 12
    # After a term closes with no winner and the same online roster, wait
    # before opening another term. The first failover term is not delayed:
    # a live leader clears election_until, so lease expiry can campaign at once.
    ELECTION_BACKOFF = 30
    ELECTION_BACKOFF_CAP = 120
    MAX_PROTOCOL = 2000
    MAX_TASKS = 100

    def __init__(self, root: Path, max_workers: int = 8,
                 public_base_path: str = "", public_origin: str = ""):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        try:
            self.max_workers = max(1, min(int(max_workers), self.MAX_WORKERS_CAP))
        except (TypeError, ValueError):
            self.max_workers = self.MAX_WORKERS_CAP
        self.public_base_path = (public_base_path or "").rstrip("/")
        self.public_origin = (public_origin or "").rstrip("/")
        self.files_dir = root / "relay-team-files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.key_path = root / "relay-team-files.key"
        self._signing_key = relay_files.load_key(self.key_path)
        self.path = root / "relay_teams.sqlite3"
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        self._condition = threading.Condition(threading.RLock())
        self._waiters = set()
        # In-memory join attempts, keyed by code hash / actor / IP. Never the code.
        self._gate_hits = {}
        self._gate_hits_lock = threading.Lock()
        self.MAX_WAITERS, self.MAX_SESSION_WAITERS = _resolve_waiter_limits()
        with self._write() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS team_sessions (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL,
                  status TEXT NOT NULL, owner_key TEXT NOT NULL,
                  created REAL NOT NULL, updated REAL NOT NULL,
                  last_seq INTEGER NOT NULL DEFAULT 0,
                  public_read INTEGER NOT NULL DEFAULT 0, public_token_hash TEXT,
                  visibility TEXT NOT NULL DEFAULT 'public', public_id TEXT,
                  leader_role TEXT, leader_seat INTEGER, leader_name TEXT, leader_key TEXT,
                  leader_term INTEGER NOT NULL DEFAULT 0, leader_lease REAL,
                  election_until REAL, protocol_seq INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS team_sessions_owner ON team_sessions(owner_key,status);
                CREATE TABLE IF NOT EXISTS team_members (
                  session_id TEXT NOT NULL, role TEXT NOT NULL, seat INTEGER NOT NULL,
                  actor_key TEXT NOT NULL, name TEXT NOT NULL, last_seen REAL NOT NULL,
                  ack_seq INTEGER NOT NULL DEFAULT 0, environment TEXT,
                  PRIMARY KEY(session_id,role,seat), UNIQUE(session_id,actor_key));
                CREATE INDEX IF NOT EXISTS team_members_actor ON team_members(actor_key,session_id);
                CREATE TABLE IF NOT EXISTS team_events (
                  session_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
                  created REAL NOT NULL, sender_role TEXT NOT NULL, sender_seat INTEGER NOT NULL,
                  sender_name TEXT NOT NULL, payload TEXT NOT NULL, sender_key TEXT,
                  request_id TEXT, content_hash TEXT,
                  PRIMARY KEY(session_id,seq), UNIQUE(session_id,sender_key,request_id));
                CREATE TABLE IF NOT EXISTS team_files (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, name TEXT NOT NULL,
                  size INTEGER NOT NULL, sha256 TEXT NOT NULL, type TEXT NOT NULL,
                  uploader TEXT NOT NULL, actor_key TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS team_files_session ON team_files(session_id);
                CREATE TABLE IF NOT EXISTS team_revoked (
                  session_id TEXT NOT NULL, role TEXT NOT NULL, seat INTEGER NOT NULL,
                  actor_key TEXT NOT NULL, revoked_at REAL NOT NULL,
                  PRIMARY KEY(session_id,role,seat));
                CREATE INDEX IF NOT EXISTS team_revoked_actor ON team_revoked(actor_key,session_id);
                CREATE TABLE IF NOT EXISTS team_invites (
                  id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL,
                  role TEXT NOT NULL, seat INTEGER, note TEXT NOT NULL DEFAULT '',
                  created REAL NOT NULL, expires REAL NOT NULL,
                  revoked INTEGER NOT NULL DEFAULT 0, used_at REAL,
                  used_by_key TEXT, used_by_name TEXT);
                CREATE INDEX IF NOT EXISTS team_invites_session ON team_invites(session_id,role,seat);
                CREATE TABLE IF NOT EXISTS team_gates (
                  id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL,
                  role TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                  max_joins INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                  closed INTEGER NOT NULL DEFAULT 0, closed_at REAL);
                CREATE INDEX IF NOT EXISTS team_gates_session ON team_gates(session_id, role, closed);
                CREATE TABLE IF NOT EXISTS team_gate_uses (
                  id TEXT PRIMARY KEY, gate_id TEXT NOT NULL, session_id TEXT NOT NULL,
                  seat INTEGER NOT NULL, actor_key TEXT NOT NULL, actor_name TEXT NOT NULL,
                  joined_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS team_gate_uses_gate ON team_gate_uses(gate_id, joined_at);
                CREATE TABLE IF NOT EXISTS team_votes (
                  session_id TEXT NOT NULL, term INTEGER NOT NULL, voter_key TEXT NOT NULL,
                  candidate_role TEXT NOT NULL, candidate_seat INTEGER NOT NULL,
                  created REAL NOT NULL,
                  PRIMARY KEY(session_id, term, voter_key));
                CREATE TABLE IF NOT EXISTS team_protocol (
                  session_id TEXT NOT NULL, seq INTEGER NOT NULL, op TEXT NOT NULL,
                  created REAL NOT NULL, sender_role TEXT NOT NULL, sender_seat INTEGER NOT NULL,
                  sender_name TEXT NOT NULL, payload TEXT NOT NULL, sender_key TEXT,
                  PRIMARY KEY(session_id, seq));
                CREATE TABLE IF NOT EXISTS team_protocol_requests (
                  session_id TEXT NOT NULL, sender_key TEXT NOT NULL, request_id TEXT NOT NULL,
                  content_hash TEXT NOT NULL, op TEXT NOT NULL,
                  PRIMARY KEY(session_id, sender_key, request_id));
                CREATE TABLE IF NOT EXISTS team_tasks (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, title TEXT NOT NULL,
                  detail TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                  assignee_role TEXT, assignee_seat INTEGER, assignee_name TEXT, assignee_key TEXT,
                  progress INTEGER NOT NULL DEFAULT 0, parent_id TEXT,
                  created_by_role TEXT NOT NULL, created_by_seat INTEGER NOT NULL,
                  created_by_name TEXT NOT NULL, creator_key TEXT NOT NULL,
                  request_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                  created REAL NOT NULL, updated REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
                CREATE UNIQUE INDEX IF NOT EXISTS team_tasks_request
                  ON team_tasks(session_id, creator_key, request_id);
                CREATE INDEX IF NOT EXISTS team_tasks_session
                  ON team_tasks(session_id, status, created);
            """)
            for name, ddl in (
                ("leader_role", "TEXT"),
                ("leader_seat", "INTEGER"),
                ("leader_name", "TEXT"),
                ("leader_key", "TEXT"),
                ("leader_term", "INTEGER NOT NULL DEFAULT 0"),
                ("leader_lease", "REAL"),
                ("election_until", "REAL"),
                ("protocol_seq", "INTEGER NOT NULL DEFAULT 0"),
                ("election_roster", "TEXT"),
                ("election_backoff_until", "REAL"),
                ("election_failures", "INTEGER NOT NULL DEFAULT 0"),
                ("election_votes", "INTEGER"),
                ("election_needed", "INTEGER"),
                ("election_eligible", "INTEGER"),
            ):
                self._ensure_column(db, "team_sessions", name, ddl)
            # Additive migration for databases created before public watch existed.
            columns = {row[1] for row in db.execute("PRAGMA table_info(team_sessions)")}
            if "public_read" not in columns:
                db.execute("ALTER TABLE team_sessions ADD COLUMN public_read "
                           "INTEGER NOT NULL DEFAULT 0")
            if "public_token_hash" not in columns:
                db.execute("ALTER TABLE team_sessions ADD COLUMN public_token_hash TEXT")
            ensure_directory_columns(db, "team_sessions")

    # -- plumbing -------------------------------------------------------

    @staticmethod
    def _ensure_column(db, table, name, ddl):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _read(self):
        db = self._connect()
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self, notify=True):
        with self._condition:
            db = self._connect()
            committed = False
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
                committed = True
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
                if committed and notify:
                    self._condition.notify_all()

    def _access(self, db, actor, sid):
        key = _actor(actor)
        if not isinstance(sid, str) or not sid or len(sid) > 160:
            raise AuthError("没有这个团队会话或无权访问", 404)
        session = db.execute("SELECT * FROM team_sessions WHERE id=?", (sid,)).fetchone()
        member = db.execute("SELECT * FROM team_members WHERE session_id=? AND actor_key=?",
                            (sid, key)).fetchone()
        if session is None or member is None:
            raise AuthError("没有这个团队会话或无权访问", 404)
        return session, member

    def _touch(self, sid, key):
        """Count an authenticated read as presence, without waking long-pollers.

        Writes are skipped while last_seen is already inside PRESENCE_REFRESH,
        so a tight poll loop does not rewrite the row on every request.
        """
        now = time.time()
        with self._write(notify=False) as db:
            row = db.execute("SELECT last_seen FROM team_members WHERE session_id=? AND actor_key=?",
                             (sid, key)).fetchone()
            if row is not None and now - row["last_seen"] >= self.PRESENCE_REFRESH:
                db.execute("UPDATE team_members SET last_seen=? WHERE session_id=? AND actor_key=?",
                           (now, sid, key))

    def _peer(self, row, include_environment=True, session=None, now=None):
        now = time.time() if now is None else now
        item = {"role": row["role"], "seat": row["seat"], "name": row["name"],
                "last_seen": _iso(row["last_seen"]), "ack_seq": row["ack_seq"]}
        item.update(self._presence_fields(row, session, now))
        if row["role"] == "worker" and include_environment:
            item["environment"] = json.loads(row["environment"]) if row["environment"] else None
        return item

    @classmethod
    def _public_peer(cls, row):
        """Anonymous-safe member view: no environment, timestamps or ack progress."""
        if row is None:
            return None
        return {"role": row["role"], "seat": row["seat"], "name": row["name"],
                "online": time.time() - row["last_seen"] < cls.ONLINE_SECONDS}

    def _session(self, db, session, member):
        rows = db.execute("SELECT * FROM team_members WHERE session_id=? ORDER BY role,seat",
                          (session["id"],)).fetchall()
        owner = next(row for row in rows if row["role"] == "owner")
        mentor = next((row for row in rows if row["role"] == "mentor"), None)
        workers = [row for row in rows if row["role"] == "worker"]
        roster = [self._roster_member(row, session) for row in rows]
        return {"id": session["id"], "title": session["title"], "goal": session["goal"],
                "status": session["status"], "created_at": _iso(session["created"]),
                "updated_at": _iso(session["updated"]), "last_seq": session["last_seq"],
                "protocol_seq": int(session["protocol_seq"] or 0),
                "owner": member["role"] == "owner", "owner_name": owner["name"],
                "role": member["role"], "seat": member["seat"],
                "public_read": bool(session["public_read"]),
                "visibility": session["visibility"] or "public",
                "public_id": session["public_id"],
                "max_workers": self.max_workers,
                "leader": self._leader_public(db, session),
                "members": roster,
                "online": [item for item in roster if item["online"]],
                "live": [item for item in roster if item["live"]],
                "tasks": [self._task_json(row) for row in self._task_rows(db, session["id"])],
                "mentor": self._peer(mentor, session=session) if mentor else None,
                "workers": [self._peer(row, member["role"] != "worker"
                                       or row["seat"] == member["seat"], session)
                            for row in workers]}

    def _event(self, db, row):
        result = {"seq": row["seq"], "kind": row["kind"], "created_at": _iso(row["created"]),
                  "sender": {"role": row["sender_role"], "seat": row["sender_seat"],
                             "name": row["sender_name"]}}
        payload = json.loads(row["payload"])
        message = payload.get("message")
        if isinstance(message, dict):
            ids = message.get("attachments") or []
            message["attachments"] = [item for item in
                                      (self._file_json(db, row["session_id"], file_id) for file_id in ids)
                                      if item is not None]
        result.update(payload)
        return result

    def _append(self, db, sid, member, kind, payload, *, request_id=None, content_hash=None,
                critical=False):
        last = db.execute("SELECT last_seq FROM team_sessions WHERE id=?", (sid,)).fetchone()[0]
        if not critical and last >= self.MAX_EVENTS - 1:
            raise AuthError("团队会话事件数量已达上限，请结束后新建", 409)
        now = time.time()
        seq = last + 1
        db.execute("UPDATE team_sessions SET last_seq=?,updated=? WHERE id=?", (seq, now, sid))
        db.execute("INSERT INTO team_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, seq, kind, now, member["role"], member["seat"], member["name"],
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    member["actor_key"], request_id, content_hash))
        return self._event(db, db.execute(
            "SELECT * FROM team_events WHERE session_id=? AND seq=?", (sid, seq)).fetchone())

    # -- session lifecycle ---------------------------------------------

    def create(self, actor, body):
        key = _actor(actor)
        body = _body(body, {"title", "goal"})
        title = _text(body.get("title"), "标题", 160)
        goal = _text(body.get("goal"), "目标", 8000)
        now = time.time()
        sid = "tm_" + secrets.token_hex(16)
        with self._write() as db:
            public_id = _mint_public_id(db, "team_sessions")
            db.execute("INSERT INTO team_sessions "
                       "(id,title,goal,status,owner_key,created,updated,last_seq,visibility,public_id) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (sid, title, goal, "active", key, now, now, 0, "public", public_id))
            db.execute("INSERT INTO team_members VALUES (?,?,?,?,?,?,?,?)",
                       (sid, "owner", 0, key, _name(actor), now, 0, None))
            return {"session": self._session(db, db.execute(
                "SELECT * FROM team_sessions WHERE id=?", (sid,)).fetchone(),
                db.execute("SELECT * FROM team_members WHERE session_id=? AND actor_key=?",
                           (sid, key)).fetchone())}

    def _ensure_can_join(self, db, session, key):
        if session is None or session["status"] == "closed":
            raise AuthError("团队会话已结束，不能加入", 409)
        if db.execute("SELECT 1 FROM team_revoked WHERE session_id=? AND actor_key=?",
                      (session["id"], key)).fetchone():
            raise AuthError("该身份已被撤销，不能重新加入团队会话", 403)
        if db.execute("SELECT 1 FROM team_members WHERE session_id=? AND actor_key=?",
                      (session["id"], key)).fetchone():
            raise AuthError("该身份已参与团队会话", 409)

    def _worker_seat_sets(self, db, sid):
        current = {row[0] for row in db.execute(
            "SELECT seat FROM team_members WHERE session_id=? AND role='worker'", (sid,))}
        retired = {row[0] for row in db.execute(
            "SELECT seat FROM team_revoked WHERE session_id=? AND role='worker'", (sid,))}
        return current, retired

    def _allocate_worker_seat(self, db, sid, preferred):
        """Pick a worker seat. Numbers already used, including revoked ones, are never reused."""
        current, retired = self._worker_seat_sets(db, sid)
        if len(current) >= self.max_workers:
            raise AuthError("执行端席位已满", 409)
        if preferred is not None:
            if isinstance(preferred, bool) or not isinstance(preferred, int) or not 1 <= preferred <= self.SEAT_CAP:
                raise ValueError("worker 席位超出范围")
            if preferred in current:
                raise AuthError("该席位已被占用", 409)
            if preferred in retired:
                raise AuthError("该席位已撤销，席位号不会再分配", 409)
            return preferred
        taken = current | retired
        seat = 1
        while seat in taken:
            seat += 1
        if seat > self.SEAT_CAP:
            raise AuthError("执行端席位已满", 409)
        return seat

    def _prepare_join(self, db, session, key, role, body, preferred_seat):
        """Validate the role payload and pick a seat. Does not insert."""
        self._ensure_can_join(db, session, key)
        if role == "mentor":
            if "environment" in body:
                raise AuthError("只有执行端可以上报系统环境", 403)
            if db.execute("SELECT 1 FROM team_members WHERE session_id=? AND role='mentor'",
                          (session["id"],)).fetchone():
                raise AuthError("指导端席位已被占用", 409)
            return 0, None
        if not body.get("environment"):
            raise ValueError("执行端加入必须上报 environment"
                             "（os_name、os_version、architecture）")
        environment = _environment(body.get("environment"))
        return self._allocate_worker_seat(db, session["id"], preferred_seat), environment

    def _insert_member(self, db, session, role, seat, key, actor, environment):
        now = time.time()
        db.execute("INSERT INTO team_members VALUES (?,?,?,?,?,?,?,?)",
                   (session["id"], role, seat, key, _name(actor), now, 0,
                    json.dumps(environment, sort_keys=True, ensure_ascii=False)
                    if environment else None))
        member = db.execute("SELECT * FROM team_members WHERE session_id=? AND actor_key=?",
                            (session["id"], key)).fetchone()
        self._append(db, session["id"], member, "joined", {"role": role, "seat": seat})
        return member, now

    def join(self, actor, body):
        key = _actor(actor)
        body = _body(body, {"code", "role", "environment"})
        code = _text(body.get("code"), "邀请码", 160)
        hashed = hashlib.sha256(code.encode()).hexdigest()
        with self._write() as db:
            invite = db.execute("SELECT * FROM team_invites WHERE code_hash=?", (hashed,)).fetchone()
            if invite is None:
                raise AuthError("邀请码无效", 403)
            if invite["expires"] <= time.time():
                raise AuthError("邀请码已过期", 403)
            if invite["revoked"]:
                raise AuthError("邀请码已被撤销", 403)
            if invite["used_at"] is not None:
                raise AuthError("邀请码已被使用", 403)
            if body.get("role") is not None and body.get("role") != invite["role"]:
                raise ValueError("所选角色与此邀请码不一致")
            session = db.execute("SELECT * FROM team_sessions WHERE id=?",
                                 (invite["session_id"],)).fetchone()
            seat, environment = self._prepare_join(
                db, session, key, invite["role"], body, invite["seat"])
            member, now = self._insert_member(
                db, session, invite["role"], seat, key, actor, environment)
            db.execute("UPDATE team_invites SET used_at=?, used_by_key=?, used_by_name=? WHERE id=?",
                       (now, key, _name(actor), invite["id"]))
            return {"session": self._session(db, db.execute(
                "SELECT * FROM team_sessions WHERE id=?", (session["id"],)).fetchone(), member)}

    def list_sessions(self, actor):
        key = _actor(actor)
        with self._read() as db:
            rows = db.execute("SELECT s.* FROM team_sessions s JOIN team_members m "
                              "ON m.session_id=s.id WHERE m.actor_key=? ORDER BY s.updated DESC",
                              (key,)).fetchall()
            result = []
            for row in rows:
                member = db.execute("SELECT * FROM team_members WHERE session_id=? AND actor_key=?",
                                    (row["id"], key)).fetchone()
                result.append(self._session(db, row, member))
            return {"sessions": result}

    def get(self, actor, sid):
        self._touch(sid, _actor(actor))
        with self._read() as db:
            session, member = self._access(db, actor, sid)
            return {"session": self._session(db, session, member)}

    # -- public read-only view ------------------------------------------

    def _public_row(self, db, hashed):
        row = db.execute("SELECT * FROM team_sessions "
                         "WHERE public_token_hash=? AND public_read=1", (hashed,)).fetchone()
        if row is None:
            raise AuthError("公开视图不可用或已关闭", 404)
        return row

    def _public_view(self, db, session):
        """Public session summary: role/seat/name only, never environment or credentials."""
        rows = db.execute("SELECT role,seat,name FROM team_members WHERE session_id=? "
                          "ORDER BY role,seat", (session["id"],)).fetchall()
        mentor = next((row for row in rows if row["role"] == "mentor"), None)
        workers = [{"role": "worker", "seat": row["seat"], "name": row["name"]}
                   for row in rows if row["role"] == "worker"]
        return {"title": session["title"], "goal": session["goal"],
                "status": session["status"], "created_at": _iso(session["created"]),
                "updated_at": _iso(session["updated"]), "last_seq": session["last_seq"],
                "max_workers": self.max_workers,
                "leader": self._public_leader(db, session),
                "mentor": ({"role": "mentor", "seat": 0, "name": mentor["name"]}
                           if mentor else None),
                "workers": workers}

    def _public_event(self, db, row):
        return self._filtered_public_event(db, row)

    def _public_page(self, token, after):
        if not isinstance(token, str) or not token or len(token) > 160:
            raise AuthError("公开视图不可用或已关闭", 404)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after须为非负整数")
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with self._read() as db:
            session = self._public_row(db, hashed)
            if after > session["last_seq"]:
                raise ValueError("消息游标超过当前会话进度")
            rows = db.execute("SELECT * FROM team_events WHERE session_id=? AND seq>? "
                              "ORDER BY seq LIMIT ?", (session["id"], after,
                                                       self.PAGE_SIZE + 1)).fetchall()
            scanned = rows[:self.PAGE_SIZE]
            events = [event for event in (self._public_event(db, row) for row in scanned)
                      if event is not None]
            return {"session": self._public_view(db, session), "events": events,
                    "next_cursor": scanned[-1]["seq"] if scanned else after,
                    "has_more": len(rows) > self.PAGE_SIZE}

    def public_events(self, token, after=0, wait=0, abandon=None):
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or not 0 <= wait <= 20:
            raise ValueError("wait须为0至20秒")
        hashed = hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) else ""
        waiter = None
        registered = False
        deadline = time.monotonic() + wait
        try:
            with self._condition:
                while True:
                    result = self._public_page(token, after)
                    if _long_poll_ready(result, wait, deadline, abandon):
                        # Re-check after waiting: disabling or rotating must close the link.
                        with self._read() as db:
                            session = self._public_row(db, hashed)
                            result["session"] = self._public_view(db, session)
                        return result
                    if result["next_cursor"] > after:
                        after = result["next_cursor"]
                        continue
                    if not registered:
                        with self._read() as db:
                            public_sid = self._public_row(db, hashed)["id"]
                        waiter = (public_sid, "public:" + hashed)
                        _claim_waiter(self._waiters, waiter, self.MAX_WAITERS, self.MAX_SESSION_WAITERS)
                        registered = True
                    self._condition.wait(min(1.0, max(0, deadline - time.monotonic())))
        finally:
            if registered:
                with self._condition:
                    self._waiters.discard(waiter)

    def set_public(self, actor, sid, body):
        body = _body(body, {"enabled"})
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            if member["role"] != "owner":
                raise AuthError("只有团队创建者可以开关公开围观", 403)
            now = time.time()
            url = None
            if enabled:
                token = secrets.token_urlsafe(24)
                db.execute("UPDATE team_sessions SET public_read=1, public_token_hash=?, "
                           "updated=? WHERE id=?",
                           (hashlib.sha256(token.encode()).hexdigest(), now, sid))
                url = self._base_url() + "/relay/watch/" + token
            else:
                db.execute("UPDATE team_sessions SET public_read=0, public_token_hash=NULL, "
                           "updated=? WHERE id=?", (now, sid))
            view = self._view(db, actor, sid)
            return {"session": view, "public_url": url}

    # -- public directory (anonymous, no long-poll) ---------------------

    PUBLIC_MARK = "t"

    def _directory_item(self, db, session, summary=True):
        rows = db.execute("SELECT role,seat,name FROM team_members WHERE session_id=?",
                          (session["id"],)).fetchall()
        order = {"owner": 0, "mentor": 1, "worker": 2}
        participants = []
        for row in sorted(rows, key=lambda item: (order.get(item["role"], 9), item["seat"])):
            participants.append({"role": row["role"], "seat": row["seat"], "name": row["name"]})
        goal = session["goal"]
        item = {"public_id": session["public_id"], "mode": "team", "title": session["title"],
                "goal": _preview(goal) if summary else goal, "status": session["status"],
                "created_at": _iso(session["created"]), "updated_at": _iso(session["updated"]),
                "participants": participants, "leader": self._public_leader(db, session),
                "last_message": self._work_preview(db, session["id"])}
        if not summary:
            item["tasks"] = [self._task_json(row) for row in self._task_rows(db, session["id"])]
            item["leader_terms"] = self._leader_terms(db, session["id"])
        return item

    def directory_candidates(self, limit, cursor_updated=None, cursor_public_id=None, status=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 51:
            raise ValueError("limit须为1至50")
        with self._read() as db:
            rows = directory_rows(db, "team_sessions", limit, cursor_updated, cursor_public_id, status)
            return [(row["updated"], row["public_id"], self._directory_item(db, row, summary=True))
                    for row in rows]

    def directory_conversation(self, public_id):
        if not isinstance(public_id, str) or PUBLIC_ID_RE.fullmatch(public_id) is None:
            return None
        if public_id[2] != self.PUBLIC_MARK:
            return None
        with self._read() as db:
            session = db.execute(
                "SELECT * FROM team_sessions WHERE public_id=? AND visibility='public'",
                (public_id,)).fetchone()
            if session is None:
                return None
            return self._directory_item(db, session, summary=False)

    def _directory_event(self, db, row, public_id):
        return self._filtered_public_event(db, row, public_id=public_id)

    def directory_events(self, public_id, after, limit):
        if (not isinstance(public_id, str) or PUBLIC_ID_RE.fullmatch(public_id) is None
                or public_id[2] != self.PUBLIC_MARK):
            return None
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after须为非负整数")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit须为1至100")
        with self._read() as db:
            session = db.execute(
                "SELECT * FROM team_sessions WHERE public_id=? AND visibility='public'",
                (public_id,)).fetchone()
            if session is None:
                return None
            if after > session["last_seq"]:
                raise ValueError("消息游标超过当前会话进度")
            rows = db.execute("SELECT * FROM team_events WHERE session_id=? AND seq>? "
                              "ORDER BY seq LIMIT ?",
                              (session["id"], after, limit + 1)).fetchall()
            scanned = rows[:limit]
            events = [event for event in (self._directory_event(db, row, public_id) for row in scanned)
                      if event is not None]
            return {"conversation": self._directory_item(db, session, summary=False),
                    "events": events,
                    "next_cursor": scanned[-1]["seq"] if scanned else after,
                    "has_more": len(rows) > limit}

    def locate_public_file(self, public_id, file_id):
        if (not isinstance(public_id, str) or PUBLIC_ID_RE.fullmatch(public_id) is None
                or public_id[2] != self.PUBLIC_MARK or not isinstance(file_id, str)
                or FILE_ID_RE.fullmatch(file_id) is None):
            raise AuthError("没有这个公开附件", 404)
        with self._read() as db:
            session = db.execute(
                "SELECT id FROM team_sessions WHERE public_id=? AND visibility='public'",
                (public_id,)).fetchone()
            if session is None:
                raise AuthError("没有这个公开附件", 404)
            row = db.execute("SELECT name,size,type FROM team_files WHERE id=? AND session_id=?",
                             (file_id, session["id"])).fetchone()
            if row is None:
                raise AuthError("没有这个公开附件", 404)
            meta = {"name": row["name"], "size": row["size"], "type": row["type"],
                    "inline": row["type"].startswith(("image/", "video/")) or row["type"] == "text/plain"}
        path = self.files_dir / file_id
        if not path.is_file():
            raise AuthError("附件已不存在", 404)
        return meta, path

    # -- events ---------------------------------------------------------

    def _on_work_stream(self, row):
        """Work poll: instructions, results, tasks, membership. Not env or election."""
        if row["kind"] == "environment" or self._protocol_note_row(row):
            return False
        return row["kind"] in ("message", "task", "joined", "state", "revoked")

    def _page(self, actor, sid, after, stream="work", include_view=True):
        with self._read() as db:
            session, member = self._access(db, actor, sid)
            snapshot = (self._session(db, session, member) if include_view
                        else self._light_session(db, session, member))
            if stream == "system":
                last = int(session["protocol_seq"] or 0)
                if after > last:
                    raise ValueError("消息游标超过当前会话进度")
                rows = db.execute("SELECT * FROM team_protocol WHERE session_id=? AND seq>? "
                                  "ORDER BY seq LIMIT ?",
                                  (sid, after, self.PAGE_SIZE + 1)).fetchall()
                scanned = rows[:self.PAGE_SIZE]
                return {"session": snapshot, "stream": "system",
                        "events": [self._protocol_event(row) for row in scanned],
                        "next_cursor": scanned[-1]["seq"] if scanned else after,
                        "has_more": len(rows) > self.PAGE_SIZE}
            if after > session["last_seq"]:
                raise ValueError("消息游标超过当前会话进度")
            rows = db.execute("SELECT * FROM team_events WHERE session_id=? AND seq>? "
                              "ORDER BY seq LIMIT ?", (sid, after, self.PAGE_SIZE + 1)).fetchall()
            scanned = rows[:self.PAGE_SIZE]
            visible = [row for row in scanned if self._on_work_stream(row)]
            return {"session": snapshot, "stream": "work",
                    "events": [self._event(db, row) for row in visible],
                    "next_cursor": scanned[-1]["seq"] if scanned else after,
                    "has_more": len(rows) > self.PAGE_SIZE}

    def events(self, actor, sid, after=0, wait=0, abandon=None, stream="work", view=1):
        key = _actor(actor)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after须为非负整数")
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or not 0 <= wait <= 20:
            raise ValueError("wait须为0至20秒")
        if not isinstance(stream, str) or stream not in ("work", "system"):
            raise ValueError("stream须为 work 或 system")
        if isinstance(view, bool) or view not in (0, 1):
            raise ValueError("view须为 0 或 1")
        include_view = view == 1
        waiter = (sid, key)
        registered = False
        deadline = time.monotonic() + wait
        try:
            with self._condition:
                while True:
                    # Refresh before the read and on each wake, so a long poll
                    # that blocks for the whole lease does not age out of quorum.
                    # notify stays off: presence is not a work-stream event.
                    self._touch(sid, key)
                    result = self._page(actor, sid, after, stream, include_view)
                    if _long_poll_ready(result, wait, deadline, abandon):
                        with self._read() as db:
                            result["session"] = (self._view(db, actor, sid) if include_view
                                                 else self._light_view(db, actor, sid))
                        return result
                    if result["next_cursor"] > after:
                        # Invisible events still advance the global cursor; skip them
                        # without sleeping so a worker is not stuck on hidden traffic.
                        after = result["next_cursor"]
                        continue
                    if not registered:
                        _claim_waiter(self._waiters, waiter, self.MAX_WAITERS, self.MAX_SESSION_WAITERS)
                        registered = True
                    self._condition.wait(min(1.0, max(0, deadline - time.monotonic())))
        finally:
            if registered:
                with self._condition:
                    self._waiters.discard(waiter)

    def _view(self, db, actor, sid):
        session, member = self._access(db, actor, sid)
        return self._session(db, session, member)

    def _light_session(self, db, session, member):
        """Cursor payload for view=0. No task board, roster, or environment."""
        return {"id": session["id"], "status": session["status"],
                "last_seq": session["last_seq"],
                "protocol_seq": int(session["protocol_seq"] or 0),
                "role": member["role"], "seat": member["seat"],
                "leader": self._leader_public(db, session),
                "updated_at": _iso(session["updated"])}

    def _light_view(self, db, actor, sid):
        session, member = self._access(db, actor, sid)
        return self._light_session(db, session, member)

    def send(self, actor, sid, body):
        body = _body(body, {"kind", "text", "request_id", "reply_to", "to", "attachments", "task_id"})
        kind = body.get("kind")
        if kind not in ("instruction", "progress", "result", "question", "note"):
            raise ValueError("未知消息类型")
        text = _text(body.get("text"), "消息", 8000)
        request_id = _text(body.get("request_id"), "request_id", 160)
        if kind == "note" and protocol_op(text):
            if any(body.get(key) is not None for key in ("reply_to", "to", "task_id")) or body.get("attachments"):
                raise ValueError("选举与心跳备注只包含协议文本")
            return self._ingest_protocol_note(actor, sid, text, request_id)
        reply = body.get("reply_to")
        if reply is not None and (isinstance(reply, bool) or not isinstance(reply, int) or reply < 1):
            raise ValueError("reply_to须为本会话消息序号")
        to = body.get("to")
        if to is not None:
            if not isinstance(to, str) or not (to == "all" or to.startswith("worker:")):
                raise ValueError('to 必须是 "all" 或 "worker:N"')
            if to != "all":
                suffix = to.split(":", 1)[1]
                if not suffix.isdigit() or not 1 <= int(suffix) <= self.SEAT_CAP:
                    raise ValueError('to 必须是 "all" 或 "worker:N"')
        attachments = body.get("attachments", [])
        if (not isinstance(attachments, list) or len(attachments) > self.MAX_ATTACHMENTS
                or any(not isinstance(item, str) for item in attachments)):
            raise ValueError(f"附件最多{self.MAX_ATTACHMENTS}个，且须为附件编号")
        attachments = list(dict.fromkeys(attachments))
        message = {"kind": kind, "text": text, "reply_to": reply}
        if to is not None:
            message["to"] = to
        if attachments:
            message["attachments"] = attachments
        task_id = body.get("task_id")
        if task_id is not None:
            if not isinstance(task_id, str) or not task_id.startswith("tt_") or len(task_id) > 80:
                raise ValueError("task_id不合法")
            message["task_id"] = task_id
        hashed = hashlib.sha256(json.dumps(message, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            duplicate = db.execute("SELECT * FROM team_events WHERE session_id=? AND sender_key=? "
                                   "AND request_id=?", (sid, member["actor_key"], request_id)).fetchone()
            if duplicate:
                if duplicate["content_hash"] != hashed:
                    raise AuthError("相同request_id不能对应不同消息", 409)
                return {"event": self._event(db, duplicate), "duplicate": True}
            if session["status"] == "closed":
                raise AuthError("团队会话已结束，不能发送新消息", 409)
            leader = self._is_leader_member(session, member)
            if kind == "instruction" and member["role"] not in ("mentor", "owner") and not leader:
                raise AuthError("只有指挥端、领导者或管理者可以发送指令", 403)
            if kind == "progress" and member["role"] != "worker":
                raise AuthError("只有执行端可以回报进度", 403)
            if kind == "instruction" and session["status"] == "paused":
                raise AuthError("团队会话已暂停，不能发送新指令", 409)
            if reply is not None and (reply > session["last_seq"] or not db.execute(
                    "SELECT 1 FROM team_events WHERE session_id=? AND seq=?", (sid, reply)).fetchone()):
                raise ValueError("reply_to不属于本团队会话历史")
            if to and to.startswith("worker:") and member["role"] not in ("mentor", "owner") and not leader:
                raise AuthError("执行端不能定向到具体席位；需要广播请用 to=\"all\"", 403)
            if to and to.startswith("worker:"):
                target = int(to.split(":", 1)[1])
                if not db.execute("SELECT 1 FROM team_members WHERE session_id=? AND role='worker' "
                                  "AND seat=?", (sid, target)).fetchone():
                    if db.execute("SELECT 1 FROM team_revoked WHERE session_id=? AND role='worker' "
                                  "AND seat=?", (sid, target)).fetchone():
                        raise AuthError("该席位已被撤销", 409)
                    raise ValueError("目标席位不存在")
            if attachments:
                placeholders = ",".join("?" for _ in attachments)
                found = db.execute(f"SELECT COUNT(*) FROM team_files WHERE session_id=? AND id IN ({placeholders})",
                                   (sid, *attachments)).fetchone()[0]
                if found != len(attachments):
                    raise ValueError("附件不属于本团队会话")
            if task_id and not db.execute("SELECT 1 FROM team_tasks WHERE id=? AND session_id=?",
                                          (task_id, sid)).fetchone():
                raise ValueError("任务不属于本团队会话")
            if kind == "result" and reply is not None and member["role"] == "worker" and not leader:
                target_row = db.execute("SELECT * FROM team_events WHERE session_id=? AND seq=?",
                                        (sid, reply)).fetchone()
                target_message = (json.loads(target_row["payload"]).get("message") or {}) if target_row else {}
                target_to = target_message.get("to")
                if (target_message.get("kind") == "instruction" and isinstance(target_to, str)
                        and target_to.startswith("worker:")
                        and int(target_to.split(":", 1)[1]) != member["seat"]):
                    raise AuthError("不能回报给其他席位的定向指令", 403)
            db.execute("UPDATE team_members SET last_seen=? WHERE session_id=? AND actor_key=?",
                       (time.time(), sid, member["actor_key"]))
            event = self._append(db, sid, member, "message", {"message": message},
                                 request_id=request_id, content_hash=hashed)
            return {"event": event, "duplicate": False}

    def ack(self, actor, sid, body):
        body = _body(body, {"seq"})
        seq = body.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError("seq须为非负整数")
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            if seq > session["last_seq"]:
                raise ValueError("确认序号超过当前会话进度")
            seq = max(seq, member["ack_seq"])
            db.execute("UPDATE team_members SET ack_seq=?,last_seen=? WHERE session_id=? AND actor_key=?",
                       (seq, time.time(), sid, member["actor_key"]))
            return {"acknowledged": seq}
    def state(self, actor, sid, body):
        body = _body(body, {"status"})
        status = body.get("status")
        if status not in ("active", "paused", "closed"):
            raise ValueError("未知团队会话状态")
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            if member["role"] != "owner":
                raise AuthError("只有团队创建者可以改变状态", 403)
            if session["status"] == "closed" and status != "closed":
                raise AuthError("已结束的团队会话不能重新开启", 409)
            if status != session["status"]:
                self._append(db, sid, member, "state", {"status": status}, critical=True)
                db.execute("UPDATE team_sessions SET status=? WHERE id=?", (status, sid))
            return {"session": self._view(db, actor, sid)}

    def heartbeat(self, actor, sid, body=None):
        body = _body({} if body is None else body, {"environment"})
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            now = time.time()
            if "environment" in body:
                if member["role"] != "worker":
                    raise AuthError("只有执行端可以上报系统环境", 403)
                environment = _environment(body["environment"])
                current = json.loads(member["environment"]) if member["environment"] else None
                if environment != current:
                    self._append(db, sid, member, "environment",
                                 {"role": "worker", "seat": member["seat"], "environment": environment})
                    db.execute("UPDATE team_members SET environment=? WHERE session_id=? AND actor_key=?",
                               (json.dumps(environment, sort_keys=True, ensure_ascii=False), sid,
                                member["actor_key"]))
            if self._is_leader_member(session, member, now):
                db.execute("UPDATE team_sessions SET leader_lease=? WHERE id=?",
                           (now + self.LEASE_SECONDS, sid))
            db.execute("UPDATE team_members SET last_seen=? WHERE session_id=? AND actor_key=?",
                       (now, sid, member["actor_key"]))
            return {"session": self._view(db, actor, sid)}

    def revoke(self, actor, sid, body):
        body = _body(body, {"role", "seat"})
        role = body.get("role")
        if role not in ("mentor", "worker"):
            raise ValueError("角色须为 mentor 或 worker")
        seat = body.get("seat")
        if seat is not None and (isinstance(seat, bool) or not isinstance(seat, int)):
            raise ValueError("seat 必须是整数")
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            if member["role"] != "owner":
                raise AuthError("只有团队创建者可以撤销席位", 403)
            if role == "mentor":
                if seat not in (None, 0):
                    raise ValueError("指导端席位固定为 0")
                targets = [0]
            elif seat is None:
                targets = [row[0] for row in db.execute(
                    "SELECT seat FROM team_members WHERE session_id=? AND role='worker'",
                    (sid,))]
            else:
                if not 1 <= seat <= self.SEAT_CAP:
                    raise ValueError("worker 席位超出范围")
                targets = [seat]
            revoked = []
            for target in targets:
                row = db.execute("SELECT * FROM team_members WHERE session_id=? AND role=? AND seat=?",
                                 (sid, role, target)).fetchone()
                if row is None:
                    continue
                if session["status"] != "closed":
                    self._append(db, sid, member, "revoked",
                                 {"role": role, "seat": target}, critical=True)
                db.execute("DELETE FROM team_members WHERE session_id=? AND role=? AND seat=?",
                           (sid, role, target))
                if session["leader_key"] == row["actor_key"]:
                    self._clear_leader(db, sid, time.time())
                    self._append_protocol(db, sid, member, "leader_lost", {
                        "op": "leader_lost", "term": int(session["leader_term"] or 0), "leader": None})
                    session = self._load_session(db, sid)
                db.execute("INSERT INTO team_revoked VALUES (?,?,?,?,?) "
                           "ON CONFLICT(session_id,role,seat) DO UPDATE SET actor_key=excluded.actor_key, "
                           "revoked_at=excluded.revoked_at",
                           (sid, role, target, row["actor_key"], time.time()))
                db.execute("UPDATE team_invites SET revoked=1 WHERE session_id=? AND role=? AND seat=? "
                           "AND used_at IS NULL AND revoked=0", (sid, role, target))
                revoked.append(target)
            if revoked:
                db.execute("UPDATE team_sessions SET updated=? WHERE id=?", (time.time(), sid))
            return {"session": self._view(db, actor, sid), "revoked_seats": revoked}

    def _base_url(self):
        return relay_files.base_url(self.public_origin, self.public_base_path)

    def signature(self, session_id, file_id, expires_at=None):
        return relay_files.sign(self._signing_key, session_id, file_id, expires_at)

    def verify_signature(self, session_id, file_id, expires, signature):
        return relay_files.verify(self._signing_key, session_id, file_id, expires, signature)

    def signed_url(self, session_id, file_id, expires_at=None):
        expires, signature = self.signature(session_id, file_id, expires_at)
        path = f"/v1/relay/teams/{session_id}/files/{file_id}"
        return relay_files.download_url(self._base_url(), path, expires, signature)

    def _file_json(self, db, session_id, file_id):
        row = db.execute("SELECT * FROM team_files WHERE id=? AND session_id=?",
                         (file_id, session_id)).fetchone()
        if row is None:
            return None
        url = self.signed_url(session_id, row["id"])
        return {"id": row["id"], "name": row["name"], "size": row["size"],
                "sha256": row["sha256"], "type": row["type"], "uploader": row["uploader"],
                "created_at": _iso(row["created"]), "url": url, "download_url": url}

    def _files_member(self, db, actor, sid):
        key = _actor(actor)
        if not isinstance(sid, str) or not sid or len(sid) > 160:
            raise AuthError("没有这个团队会话", 404)
        session = db.execute("SELECT * FROM team_sessions WHERE id=?", (sid,)).fetchone()
        if session is None:
            raise AuthError("没有这个团队会话", 404)
        member = db.execute("SELECT * FROM team_members WHERE session_id=? AND actor_key=?",
                            (sid, key)).fetchone()
        if member is None:
            raise AuthError("不是该团队会话成员，不能访问附件", 403)
        return session, member

    def _register(self, actor, sid, name, size, digest, content_type, temporary):
        if size <= 0:
            raise ValueError("文件不能为空")
        if size > self.MAX_FILE_BYTES:
            raise ValueError("单个文件不能超过 100 MB")
        key = _actor(actor)
        with self._write() as db:
            session, member = self._files_member(db, actor, sid)
            if session["status"] == "closed":
                raise AuthError("团队会话已结束，不能上传附件", 409)
            file_id = "fl_" + secrets.token_hex(12)
            target = self.files_dir / file_id
            os.replace(temporary, target)
            try:
                db.execute("INSERT INTO team_files VALUES (?,?,?,?,?,?,?,?,?)",
                           (file_id, sid, name, size, digest, content_type,
                            member["name"], key, time.time()))
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return self._file_json(db, sid, file_id)

    def receive(self, actor, sid, name, reader, length):
        name = relay_files.safe_name(name)
        if length <= 0:
            raise ValueError("文件不能为空")
        if length > self.MAX_FILE_BYTES:
            raise ValueError("单个文件不能超过 100 MB")
        with self._read() as db:
            self._files_member(db, actor, sid)
        temporary = self.files_dir / (".upload-" + secrets.token_hex(8))
        digest = hashlib.sha256()
        written = 0
        head = b""
        try:
            with temporary.open("wb") as output:
                while written < length:
                    chunk = reader.read(min(65536, length - written))
                    if not chunk:
                        raise ValueError("上传未完成")
                    if len(head) < 16:
                        head = (head + chunk)[:16]
                    output.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
            return self._register(actor, sid, name, written, digest.hexdigest(),
                                  relay_files.content_type(name, head), temporary)
        finally:
            temporary.unlink(missing_ok=True)

    def upload(self, actor, sid, name, content):
        if not isinstance(content, (bytes, bytearray)):
            raise ValueError("文件内容不合法")
        return self.receive(actor, sid, name, io.BytesIO(bytes(content)), len(content))

    def _locate(self, sid, file_id):
        with self._read() as db:
            row = db.execute("SELECT * FROM team_files WHERE id=? AND session_id=?",
                             (file_id, sid)).fetchone()
            if row is None:
                raise AuthError("没有这个团队附件", 404)
            meta = self._file_json(db, sid, row["id"])
        path = self.files_dir / row["id"]
        if not path.is_file():
            raise AuthError("附件已不存在", 404)
        meta["inline"] = meta["type"].startswith(("image/", "video/")) or meta["type"] == "text/plain"
        return meta, path

    def locate_file(self, actor, sid, file_id):
        with self._read() as db:
            self._files_member(db, actor, sid)
        return self._locate(sid, file_id)

    def locate_file_unsigned(self, sid, file_id):
        return self._locate(sid, file_id)

    # -- invitations ----------------------------------------------------

    def _invite_json(self, row):
        now = time.time()
        if row["used_at"] is not None:
            status = "used"
        elif row["revoked"]:
            status = "revoked"
        elif row["expires"] <= now:
            status = "expired"
        else:
            status = "unused"
        return {"id": row["id"], "role": row["role"], "seat": row["seat"],
                "note": row["note"], "status": status,
                "created_at": _iso(row["created"]), "expires_at": _iso(row["expires"]),
                "used_by": ({"name": row["used_by_name"], "at": _iso(row["used_at"])}
                            if row["used_at"] is not None else None)}

    def invite(self, actor, sid, body):
        body = _body(body, {"role", "seat", "note"})
        role = body.get("role")
        if role not in ("mentor", "worker"):
            raise ValueError("角色须为 mentor 或 worker")
        seat = body.get("seat")
        if seat is not None and (isinstance(seat, bool) or not isinstance(seat, int)):
            raise ValueError("seat 必须是整数")
        note = body.get("note", "")
        if not isinstance(note, str) or len(note) > 160:
            raise ValueError("邀请备注最多160字符")
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            if member["role"] != "owner":
                raise AuthError("只有团队创建者可以生成邀请", 403)
            if session["status"] == "closed":
                raise AuthError("团队会话已结束", 409)
            if role == "mentor":
                if seat not in (None, 0):
                    raise ValueError("指导端席位固定为 0")
                if db.execute("SELECT 1 FROM team_members WHERE session_id=? AND role='mentor'",
                              (sid,)).fetchone():
                    raise AuthError("指导端席位已被占用", 409)
                seat = 0
            else:
                if seat is not None:
                    current, retired = self._worker_seat_sets(db, sid)
                    if isinstance(seat, bool) or not isinstance(seat, int) or not 1 <= seat <= self.SEAT_CAP:
                        raise ValueError("worker 席位超出范围")
                    if seat in current:
                        raise AuthError("该席位已被占用", 409)
                    if seat in retired:
                        raise AuthError("该席位已撤销，席位号不会再分配", 409)
            code = secrets.token_urlsafe(24)
            now = time.time()
            expires = now + self.INVITE_SECONDS
            invite_id = "ti_" + secrets.token_hex(8)
            db.execute("INSERT INTO team_invites VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (invite_id, hashlib.sha256(code.encode()).hexdigest(), sid, role, seat,
                        note.strip(), now, expires, 0, None, None, None))
            row = db.execute("SELECT * FROM team_invites WHERE id=?", (invite_id,)).fetchone()
            item = self._invite_json(row)
            item["code"] = code
            return {"invite": item}

    def invites(self, actor, sid):
        with self._read() as db:
            session, member = self._access(db, actor, sid)
            if member["role"] != "owner":
                raise AuthError("只有团队创建者可以查看邀请", 403)
            rows = db.execute("SELECT * FROM team_invites WHERE session_id=? ORDER BY created DESC",
                              (sid,)).fetchall()
            return {"invites": [self._invite_json(row) for row in rows]}

    def revoke_invite(self, actor, sid, invite_id):
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            if member["role"] != "owner":
                raise AuthError("只有团队创建者可以撤销邀请", 403)
            row = db.execute("SELECT * FROM team_invites WHERE id=? AND session_id=?",
                             (invite_id, sid)).fetchone()
            if row is None:
                raise AuthError("没有这个邀请", 404)
            if row["used_at"] is not None:
                raise AuthError("邀请已被使用，需撤销该席位", 409)
            db.execute("UPDATE team_invites SET revoked=1 WHERE id=?", (invite_id,))
            return {"revoked": True, "invite": self._invite_json(
                db.execute("SELECT * FROM team_invites WHERE id=?", (invite_id,)).fetchone())}

    def reset_invites(self, actor, sid):
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            if member["role"] != "owner":
                raise AuthError("只有团队创建者可以重置邀请", 403)
            result = db.execute("UPDATE team_invites SET revoked=1 "
                                "WHERE session_id=? AND used_at IS NULL AND revoked=0", (sid,))
            return {"revoked": result.rowcount}

    def invite_info(self, code):
        """Public view of an invitation code for the join link."""
        if not isinstance(code, str):
            raise AuthError("邀请码无效", 404)
        code = code.strip()
        if not code or len(code) > 160:
            raise AuthError("邀请码无效", 404)
        hashed = hashlib.sha256(code.encode()).hexdigest()
        with self._read() as db:
            invite = db.execute("SELECT * FROM team_invites WHERE code_hash=?", (hashed,)).fetchone()
            if invite is None:
                raise AuthError("邀请码无效", 404)
            session = db.execute("SELECT * FROM team_sessions WHERE id=?",
                                 (invite["session_id"],)).fetchone()
            rows = db.execute("SELECT * FROM team_members WHERE session_id=? ORDER BY role,seat",
                              (session["id"],)).fetchall()
        mentor = next((row for row in rows if row["role"] == "mentor"), None)
        workers = [row for row in rows if row["role"] == "worker"]
        return {"title": session["title"], "goal": session["goal"],
                "status": session["status"], "created_at": _iso(session["created"]),
                "invite": self._invite_json(invite),
                "mentor": self._public_peer(mentor),
                "workers": [self._public_peer(row) for row in workers],
                "max_workers": self.max_workers}

    # -- multi-join gates ----------------------------------------------
    # One active gate per (session, role). The plaintext code is handed back
    # only from open/rotate. Status, events, and the public directory never
    # include it. Join attempts are counted by code hash, actor, and IP.

    def _owner_gate(self, db, actor, sid, denied):
        session, member = self._access(db, actor, sid)
        if member["role"] != "owner":
            raise AuthError(denied, 403)
        return session, member

    def _gate_role(self, role, *, default=None, required=False):
        if role is None:
            role = default
        if role is None:
            if required:
                raise ValueError("角色须为 mentor 或 worker")
            return None
        if role not in ("mentor", "worker"):
            raise ValueError("角色须为 mentor 或 worker")
        return role

    def _gate_ttl(self, body):
        ttl = body.get("ttl_hours", 24)
        if (isinstance(ttl, bool) or not isinstance(ttl, (int, float))
                or not math.isfinite(ttl)
                or not self.GATE_TTL_MIN_HOURS <= ttl <= self.GATE_TTL_MAX_HOURS):
            raise ValueError("ttl_hours 须为 1 到 168 的小时数")
        return float(ttl) * 3600

    def _normalize_gate_code(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > 32 or "\x00" in value:
            raise ValueError("门禁码不能为空或超过长度限制")
        return value.strip().upper()

    def _gate_code_ok(self, code):
        return (len(code) == self.GATE_CODE_LENGTH
                and all(character in self.GATE_CODE_ALPHABET for character in code))

    def _mint_gate_code(self, db):
        alphabet = self.GATE_CODE_ALPHABET
        for _ in range(8):
            code = "".join(secrets.choice(alphabet) for _ in range(self.GATE_CODE_LENGTH))
            hashed = hashlib.sha256(code.encode()).hexdigest()
            if db.execute("SELECT 1 FROM team_gates WHERE code_hash=?", (hashed,)).fetchone() is None:
                return code, hashed
        raise AuthError("暂时无法生成门禁码，请重试", 503)

    def _limit_gate_join(self, code_hash, actor_key, client_ip):
        """Count one join attempt. Keys are the code hash, the actor id, and the source IP."""
        buckets = [
            ("code:" + code_hash, self.GATE_JOIN_CODE_LIMIT),
            ("actor:" + actor_key, self.GATE_JOIN_ACTOR_LIMIT),
        ]
        if isinstance(client_ip, str) and client_ip and len(client_ip) <= 80 and "\x00" not in client_ip:
            buckets.append(("ip:" + client_ip, self.GATE_JOIN_IP_LIMIT))
        now = time.monotonic()
        window = self.GATE_JOIN_WINDOW
        retry_after = 1
        with self._gate_hits_lock:
            snapshots = {}
            blocked = False
            for key, limit in buckets:
                hits = [moment for moment in self._gate_hits.get(key, ()) if now - moment < window]
                snapshots[key] = hits
                if len(hits) >= limit:
                    blocked = True
                    retry_after = max(retry_after, int(math.ceil(window - (now - hits[0]))))
            if blocked:
                for key, hits in snapshots.items():
                    self._gate_hits[key] = hits
                raise AuthError(f"尝试过于频繁，请 {max(1, retry_after)} 秒后重试", 429,
                                retry_after=max(1, retry_after))
            for key, hits in snapshots.items():
                hits.append(now)
                self._gate_hits[key] = hits
            if len(self._gate_hits) > 4000:
                for key, hits in list(self._gate_hits.items()):
                    if not hits or now - hits[-1] >= window:
                        self._gate_hits.pop(key, None)

    def _gate_used_rows(self, db, gate_id):
        return db.execute("SELECT seat, actor_name, joined_at FROM team_gate_uses "
                          "WHERE gate_id=? ORDER BY joined_at, id", (gate_id,)).fetchall()

    def _gate_join_hint(self, code, role):
        if role == "mentor":
            return "diaodu_relay_team_gate_join gate_code=%s role=mentor" % code
        return ("diaodu_relay_team_gate_join gate_code=%s "
                "environment={os_name,os_version,architecture}" % code)

    def _gate_json(self, db, row, *, code=None):
        used_rows = self._gate_used_rows(db, row["id"])
        used = len(used_rows)
        remaining = max(0, row["max_joins"] - used)
        now = time.time()
        if row["closed"]:
            status = "closed"
        elif row["expires"] <= now:
            status = "expired"
        elif remaining <= 0:
            status = "exhausted"
        else:
            status = "open"
        session = db.execute("SELECT public_id FROM team_sessions WHERE id=?",
                             (row["session_id"],)).fetchone()
        item = {"id": row["id"], "role": row["role"], "note": row["note"], "status": status,
                "max_joins": row["max_joins"], "used": used, "remaining": remaining,
                "created_at": _iso(row["created"]), "expires_at": _iso(row["expires"]),
                "session_id": row["session_id"],
                "public_id": session["public_id"] if session else None,
                "used_by": [{"name": used_row["actor_name"], "seat": used_row["seat"],
                             "at": _iso(used_row["joined_at"])} for used_row in used_rows]}
        if code is not None:
            item["code"] = code
            item["join_hint"] = self._gate_join_hint(code, row["role"])
        return item

    def _active_gate(self, db, sid, role, now=None):
        now = time.time() if now is None else now
        return db.execute("SELECT * FROM team_gates WHERE session_id=? AND role=? "
                          "AND closed=0 AND expires>? ORDER BY created DESC LIMIT 1",
                          (sid, role, now)).fetchone()

    def _current_gate(self, db, sid, role):
        active = self._active_gate(db, sid, role)
        if active is not None:
            return active
        return db.execute("SELECT * FROM team_gates WHERE session_id=? AND role=? "
                          "ORDER BY created DESC LIMIT 1", (sid, role)).fetchone()

    def open_gate(self, actor, sid, body):
        body = _body(body, {"role", "max_joins", "ttl_hours", "note"})
        role = self._gate_role(body.get("role"), required=True)
        max_joins = body.get("max_joins")
        if isinstance(max_joins, bool) or not isinstance(max_joins, int) or not 1 <= max_joins <= self.max_workers:
            raise ValueError("max_joins 须为 1 到 %d" % self.max_workers)
        if role == "mentor" and max_joins != 1:
            raise ValueError("指导端门禁只能加入 1 次")
        note = body.get("note", "")
        if not isinstance(note, str) or len(note) > 160:
            raise ValueError("门禁备注最多160字符")
        ttl = self._gate_ttl(body)
        with self._write() as db:
            session, _member = self._owner_gate(db, actor, sid, "只有团队创建者可以开加入门禁")
            if session["status"] == "closed":
                raise AuthError("团队会话已结束", 409)
            if self._active_gate(db, sid, role) is not None:
                raise AuthError("本会话该角色已有未关闭的加入门禁，请先关闭或等待过期", 409)
            if role == "mentor" and db.execute(
                    "SELECT 1 FROM team_members WHERE session_id=? AND role='mentor'", (sid,)).fetchone():
                raise AuthError("指导端席位已被占用", 409)
            code, hashed = self._mint_gate_code(db)
            now = time.time()
            gate_id = "tg_" + secrets.token_hex(8)
            db.execute("INSERT INTO team_gates VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (gate_id, hashed, sid, role, note.strip(), max_joins, now, now + ttl, 0, None))
            row = db.execute("SELECT * FROM team_gates WHERE id=?", (gate_id,)).fetchone()
            return {"gate": self._gate_json(db, row, code=code)}

    def gate_status(self, actor, sid, role=None):
        role = self._gate_role(role, default=None)
        with self._read() as db:
            self._owner_gate(db, actor, sid, "只有团队创建者可以查看加入门禁")
            if role is None:
                gates = []
                for item_role in ("worker", "mentor"):
                    row = self._current_gate(db, sid, item_role)
                    if row is not None:
                        gates.append(self._gate_json(db, row))
                return {"gates": gates}
            row = self._current_gate(db, sid, role)
            if row is None:
                raise AuthError("没有这个加入门禁", 404)
            return {"gate": self._gate_json(db, row)}

    def close_gate(self, actor, sid, body):
        body = _body({} if body is None else body, {"role"})
        role = self._gate_role(body.get("role"), default="worker")
        with self._write() as db:
            self._owner_gate(db, actor, sid, "只有团队创建者可以关闭加入门禁")
            row = self._current_gate(db, sid, role)
            if row is None:
                raise AuthError("没有这个加入门禁", 404)
            if not row["closed"]:
                db.execute("UPDATE team_gates SET closed=1, closed_at=? WHERE id=?",
                           (time.time(), row["id"]))
                row = db.execute("SELECT * FROM team_gates WHERE id=?", (row["id"],)).fetchone()
            return {"closed": True, "gate": self._gate_json(db, row)}

    def rotate_gate(self, actor, sid, body):
        body = _body({} if body is None else body, {"role"})
        role = self._gate_role(body.get("role"), default="worker")
        with self._write() as db:
            session, _member = self._owner_gate(db, actor, sid, "只有团队创建者可以更换门禁码")
            if session["status"] == "closed":
                raise AuthError("团队会话已结束", 409)
            row = self._active_gate(db, sid, role)
            if row is None:
                raise AuthError("没有可更换的加入门禁", 409)
            code, hashed = self._mint_gate_code(db)
            db.execute("UPDATE team_gates SET code_hash=? WHERE id=?", (hashed, row["id"]))
            row = db.execute("SELECT * FROM team_gates WHERE id=?", (row["id"],)).fetchone()
            return {"gate": self._gate_json(db, row, code=code)}

    def expand_gate(self, actor, sid, body):
        """Raise max_joins on the open gate. The code does not change and is not returned."""
        body = _body(body, {"role", "max_joins"})
        role = self._gate_role(body.get("role"), default="worker")
        max_joins = body.get("max_joins")
        if (isinstance(max_joins, bool) or not isinstance(max_joins, int)
                or not 1 <= max_joins <= self.GATE_JOIN_CAP):
            raise ValueError("max_joins 须为 1 到 %d" % self.GATE_JOIN_CAP)
        if role == "mentor":
            raise ValueError("指导端门禁只能加入 1 次")
        with self._write() as db:
            session, _member = self._owner_gate(db, actor, sid, "只有团队创建者可以提高门禁名额")
            if session["status"] == "closed":
                raise AuthError("团队会话已结束", 409)
            row = self._active_gate(db, sid, role)
            if row is None:
                raise AuthError("没有可提高名额的加入门禁", 409)
            if max_joins <= row["max_joins"]:
                raise ValueError("max_joins 只能提高，不能降低或保持不变")
            db.execute("UPDATE team_gates SET max_joins=? WHERE id=?", (max_joins, row["id"]))
            row = db.execute("SELECT * FROM team_gates WHERE id=?", (row["id"],)).fetchone()
            return {"gate": self._gate_json(db, row)}

    def join_gate(self, actor, body, client_ip=None):
        key = _actor(actor)
        body = _body(body, {"gate_code", "role", "environment"})
        code = self._normalize_gate_code(body.get("gate_code"))
        hashed = hashlib.sha256(code.encode()).hexdigest()
        self._limit_gate_join(hashed, key, client_ip)
        if body.get("role") is not None and body.get("role") not in ("mentor", "worker"):
            raise ValueError("角色须为 mentor 或 worker")
        with self._write() as db:
            if not self._gate_code_ok(code):
                raise AuthError("门禁码无效", 403)
            gate = db.execute("SELECT * FROM team_gates WHERE code_hash=?", (hashed,)).fetchone()
            if gate is None:
                raise AuthError("门禁码无效", 403)
            if gate["closed"]:
                raise AuthError("门禁已关闭", 403)
            if gate["expires"] <= time.time():
                raise AuthError("门禁码已过期", 403)
            used = len(self._gate_used_rows(db, gate["id"]))
            if used >= gate["max_joins"]:
                raise AuthError("门禁加入名额已用完", 403)
            if body.get("role") is not None and body.get("role") != gate["role"]:
                raise ValueError("所选角色与此门禁不一致")
            session = db.execute("SELECT * FROM team_sessions WHERE id=?",
                                 (gate["session_id"],)).fetchone()
            seat, environment = self._prepare_join(db, session, key, gate["role"], body, None)
            member, now = self._insert_member(
                db, session, gate["role"], seat, key, actor, environment)
            db.execute("INSERT INTO team_gate_uses VALUES (?,?,?,?,?,?,?)",
                       ("tu_" + secrets.token_hex(8), gate["id"], session["id"], seat, key,
                        _name(actor), now))
            return {"session": self._session(db, db.execute(
                "SELECT * FROM team_sessions WHERE id=?", (session["id"],)).fetchone(), member)}

    # -- work surface: roster, election, tasks -------------------------

    def _roster_member(self, row, session=None, now=None):
        now = time.time() if now is None else now
        item = {"role": row["role"], "seat": row["seat"], "name": row["name"],
                "last_seen": _iso(row["last_seen"]), "ack_seq": row["ack_seq"]}
        item.update(self._presence_fields(row, session, now))
        return item

    def _load_session(self, db, sid):
        return db.execute("SELECT * FROM team_sessions WHERE id=?", (sid,)).fetchone()

    def _leader_active(self, session, now=None):
        now = time.time() if now is None else now
        lease = session["leader_lease"]
        if not session["leader_key"] or lease is None:
            return False
        return lease > now

    def _failed_leader(self, session, row, now):
        """Lease expired and this member has not been seen since it ended.

        A dead leader's last heartbeat is older than the lease end, so they must
        not stay online for ONLINE_SECONDS or keep a quorum seat.
        """
        if not session["leader_key"] or session["leader_key"] != row["actor_key"]:
            return False
        if self._leader_active(session, now):
            return False
        lease = session["leader_lease"]
        if lease is None:
            return False
        return row["last_seen"] <= lease

    def _member_online(self, row, session=None, now=None):
        now = time.time() if now is None else now
        if session is not None and self._failed_leader(session, row, now):
            return False
        return now - row["last_seen"] < self.ONLINE_SECONDS

    def _member_live(self, row, session=None, now=None):
        """Lease-window presence. This is the signal for quorum and assignment."""
        now = time.time() if now is None else now
        if session is not None and self._failed_leader(session, row, now):
            return False
        return now - row["last_seen"] < self.LEASE_SECONDS

    def _presence_fields(self, row, session, now):
        return {"online": self._member_online(row, session, now),
                "live": self._member_live(row, session, now),
                "last_seen_age_s": max(0, int(now - row["last_seen"]))}

    def _in_quorum(self, session, row, now):
        """Quorum presence uses the lease window, not the longer roster window."""
        if row["role"] not in ("mentor", "worker"):
            return False
        return self._member_live(row, session, now)

    def _leader_public(self, db, session, now=None):
        now = time.time() if now is None else now
        if not self._leader_active(session, now):
            return None
        row = db.execute("SELECT * FROM team_members WHERE session_id=? AND actor_key=?",
                         (session["id"], session["leader_key"])).fetchone()
        if row is None:
            return None
        body = {"role": row["role"], "seat": row["seat"], "name": row["name"],
                "term": int(session["leader_term"] or 0),
                "lease_until": _iso(session["leader_lease"])}
        body.update(self._presence_fields(row, session, now))
        return body

    def _public_leader(self, db, session, now=None):
        leader = self._leader_public(db, session, now)
        if leader is None:
            return None
        return {"role": leader["role"], "seat": leader["seat"], "name": leader["name"],
                "online": leader["online"], "term": leader["term"]}

    def _leader_terms(self, db, sid):
        """Public history of who held each term. Protocol rows stay off the work stream."""
        rows = db.execute("SELECT op, created, payload FROM team_protocol WHERE session_id=? "
                          "AND op IN ('elected','appoint') ORDER BY seq", (sid,)).fetchall()
        terms = []
        for row in rows:
            payload = json.loads(row["payload"])
            leader = payload.get("leader") if isinstance(payload.get("leader"), dict) else None
            if not leader or not leader.get("name"):
                continue
            terms.append({"term": payload.get("term"), "role": leader.get("role"),
                          "seat": leader.get("seat"), "name": leader.get("name"),
                          "started_at": _iso(row["created"])})
        return terms

    def _is_leader_member(self, session, member, now=None):
        return self._leader_active(session, now) and session["leader_key"] == member["actor_key"]

    def _eligible_rows(self, db, sid):
        return db.execute(
            "SELECT * FROM team_members WHERE session_id=? AND role IN ('mentor','worker') "
            "ORDER BY role, seat", (sid,)).fetchall()

    @staticmethod
    def _majority(count):
        return count // 2 + 1

    def _election_open(self, session, now):
        if self._leader_active(session, now):
            return False
        until = session["election_until"]
        return until is not None and until > now and int(session["leader_term"] or 0) > 0

    def _electorate(self, db, session, now):
        return [row for row in self._eligible_rows(db, session["id"])
                if self._in_quorum(session, row, now)]

    def _rows_from_roster(self, db, session):
        rows = []
        for piece in (session["election_roster"] or "").split(","):
            if ":" not in piece:
                continue
            role, raw = piece.split(":", 1)
            try:
                seat = int(raw)
            except ValueError:
                continue
            row = db.execute("SELECT * FROM team_members WHERE session_id=? AND role=? AND seat=?",
                             (session["id"], role, seat)).fetchone()
            if row is not None and row["role"] in ("mentor", "worker"):
                rows.append(row)
        return rows

    def _term_electorate(self, db, session, now):
        """Electorate for the open term.

        The roster is snapshotted when the term opens. A reader whose
        last_seen blips across the lease edge does not change `needed`
        until the next term. Revoked seats drop out because the member row
        is gone. Between terms the live roster is used, so a member who
        has actually left does not inflate the next election.
        """
        if self._election_open(session, now) and (session["election_roster"] or ""):
            return self._rows_from_roster(db, session)
        return self._electorate(db, session, now)

    @staticmethod
    def _roster_token(rows):
        ordered = sorted(rows, key=lambda item: (item["role"], item["seat"]))
        return ",".join(f"{item['role']}:{item['seat']}" for item in ordered)

    def _election_view(self, db, session, now=None):
        now = time.time() if now is None else now
        term = int(session["leader_term"] or 0)
        open_election = self._election_open(session, now)
        # A live lease already decided the term. Don't recompute needed against
        # whoever has joined since, or a finished election looks like it failed.
        if self._leader_active(session, now):
            stored_needed = session["election_needed"]
            if stored_needed is None:
                votes = db.execute("SELECT COUNT(*) FROM team_votes WHERE session_id=? AND term=?",
                                   (session["id"], term)).fetchone()[0]
                needed = votes if votes else 1
                eligible = needed
                votes = max(votes, needed)
            else:
                votes = int(session["election_votes"] or 0)
                needed = int(stored_needed)
                eligible = int(session["election_eligible"] if session["election_eligible"] is not None else needed)
                if votes < needed:
                    needed = votes
        else:
            electorate = self._term_electorate(db, session, now)
            votes = db.execute("SELECT COUNT(*) FROM team_votes WHERE session_id=? AND term=?",
                               (session["id"], term)).fetchone()[0]
            needed = self._majority(len(electorate)) if electorate else 0
            eligible = len(electorate)
        return {"term": term, "open": open_election, "votes": votes,
                "needed": needed, "eligible": eligible,
                "election_until": _iso(session["election_until"]) if open_election else None,
                "leader": self._leader_public(db, session, now)}

    def _clear_leader(self, db, sid, now):
        db.execute("UPDATE team_sessions SET leader_role=NULL, leader_seat=NULL, leader_name=NULL, "
                   "leader_key=NULL, leader_lease=NULL, updated=? WHERE id=?", (now, sid))

    def _blocked_by_live_leader(self, db, session, now):
        if not self._leader_active(session, now):
            return False
        row = db.execute("SELECT 1 FROM team_members WHERE session_id=? AND actor_key=?",
                         (session["id"], session["leader_key"])).fetchone()
        if row is None:
            self._clear_leader(db, session["id"], now)
            return False
        return True

    def _set_leader(self, db, sid, member, term, now, tally):
        votes, needed, eligible = tally
        db.execute("UPDATE team_sessions SET leader_role=?, leader_seat=?, leader_name=?, leader_key=?, "
                   "leader_term=?, leader_lease=?, election_until=NULL, election_backoff_until=NULL, "
                   "election_votes=?, election_needed=?, election_eligible=?, updated=? WHERE id=?",
                   (member["role"], member["seat"], member["name"], member["actor_key"],
                    term, now + self.LEASE_SECONDS, int(votes), int(needed), int(eligible), now, sid))

    def _mark_seen(self, db, sid, key, now):
        db.execute("UPDATE team_members SET last_seen=? WHERE session_id=? AND actor_key=?",
                   (now, sid, key))

    def _stall_blocks(self, db, session, now):
        """Seconds to wait before a new term, or None when a term may open.

        A failed term with the same online roster arms backoff and does not
        increment the term. A roster change (someone left the lease window,
        including a leader whose lease just expired and dropped out) may open
        immediately. The first campaign after a cleared lease is not a failed
        term: election_until is null while a leader holds office.
        """
        if self._leader_active(session, now) or self._election_open(session, now):
            return None
        term = int(session["leader_term"] or 0)
        until = session["election_until"]
        if term <= 0 or until is None or until > now:
            return None
        roster = self._roster_token(self._electorate(db, session, now))
        previous = session["election_roster"] or ""
        if roster != previous:
            db.execute("UPDATE team_sessions SET election_failures=0 WHERE id=?", (session["id"],))
            return None
        backoff_until = session["election_backoff_until"]
        if backoff_until is not None and now < float(backoff_until):
            return max(1, int(math.ceil(float(backoff_until) - now)))
        if backoff_until is not None and now >= float(backoff_until):
            return None
        failures = int(session["election_failures"] or 0)
        delay = min(self.ELECTION_BACKOFF_CAP, self.ELECTION_BACKOFF * (2 ** min(failures, 4)))
        db.execute("UPDATE team_sessions SET election_backoff_until=?, election_failures=? WHERE id=?",
                   (now + delay, failures + 1, session["id"]))
        return max(1, int(delay))

    def _open_election(self, db, session, now):
        if self._election_open(session, now):
            return int(session["leader_term"])
        session = self._load_session(db, session["id"])
        # Keep the expired leader_key and leader_lease. Quorum uses them to
        # exclude that seat until it is seen again. _leader_public stays empty
        # because the lease is not active, so callers do not observe a leader.
        roster = self._roster_token(self._electorate(db, session, now))
        term = int(session["leader_term"] or 0) + 1
        db.execute("DELETE FROM team_votes WHERE session_id=?", (session["id"],))
        db.execute("UPDATE team_sessions SET leader_term=?, election_until=?, "
                   "election_roster=?, election_backoff_until=NULL, updated=? WHERE id=?",
                   (term, now + self.ELECTION_WINDOW, roster, now, session["id"]))
        return term

    def _cast_vote(self, db, sid, term, voter, candidate):
        existing = db.execute("SELECT candidate_role, candidate_seat FROM team_votes "
                              "WHERE session_id=? AND term=? AND voter_key=?",
                              (sid, term, voter["actor_key"])).fetchone()
        if existing is not None:
            return False
        db.execute("INSERT INTO team_votes VALUES (?,?,?,?,?,?)",
                   (sid, term, voter["actor_key"], candidate["role"], candidate["seat"], time.time()))
        return True

    def _candidate_member(self, db, sid, role, seat, allowed):
        member = db.execute("SELECT * FROM team_members WHERE session_id=? AND role=? AND seat=?",
                            (sid, role, seat)).fetchone()
        if member is None or member["actor_key"] not in allowed:
            return None
        return member

    def _maybe_elect(self, db, sid, term, now):
        session = self._load_session(db, sid)
        electorate = self._term_electorate(db, session, now)
        allowed = {row["actor_key"] for row in electorate}
        needed = self._majority(len(electorate)) if electorate else 99
        rows = db.execute(
            "SELECT candidate_role, candidate_seat, COUNT(*) AS votes FROM team_votes "
            "WHERE session_id=? AND term=? GROUP BY candidate_role, candidate_seat "
            "ORDER BY votes DESC, candidate_seat ASC, candidate_role ASC",
            (sid, term)).fetchall()
        voted = {row["voter_key"] for row in db.execute(
            "SELECT voter_key FROM team_votes WHERE session_id=? AND term=?", (sid, term))}
        winner = None
        winning_votes = 0
        for row in rows:
            if row["votes"] < needed:
                break
            member = self._candidate_member(db, sid, row["candidate_role"], row["candidate_seat"], allowed)
            if member is not None:
                winner = member
                winning_votes = int(row["votes"])
                break
        # Everyone currently in the quorum has voted and nobody holds a majority
        # (two survivors each campaigned for themselves). Pick one: most votes,
        # then lowest seat. The server records a single leader, so this cannot
        # split brain. A silent member still in the quorum blocks this path.
        if winner is None and electorate and allowed <= voted:
            for row in rows:
                member = self._candidate_member(
                    db, sid, row["candidate_role"], row["candidate_seat"], allowed)
                if member is not None:
                    winner = member
                    winning_votes = int(row["votes"])
                    break
        if winner is None:
            return None
        applied_needed = needed if winning_votes >= needed else winning_votes
        self._set_leader(db, sid, winner, term, now,
                         (winning_votes, applied_needed, len(electorate)))
        return winner

    def _append_protocol(self, db, sid, member, op, payload):
        last = db.execute("SELECT protocol_seq FROM team_sessions WHERE id=?", (sid,)).fetchone()[0] or 0
        if last >= self.MAX_PROTOCOL:
            return None
        seq = int(last) + 1
        now = time.time()
        db.execute("UPDATE team_sessions SET protocol_seq=?, updated=? WHERE id=?", (seq, now, sid))
        db.execute("INSERT INTO team_protocol "
                   "(session_id,seq,op,created,sender_role,sender_seat,sender_name,payload,sender_key) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (sid, seq, op, now, member["role"], member["seat"], member["name"],
                    json.dumps(payload, ensure_ascii=False, sort_keys=True), member["actor_key"]))
        return seq

    def _protocol_event(self, row):
        payload = json.loads(row["payload"])
        leader = payload.get("leader")
        candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else None
        if isinstance(leader, dict):
            leader = {key: leader.get(key) for key in (
                "role", "seat", "name", "online", "live", "last_seen_age_s", "term", "lease_until")}
        if candidate is not None:
            candidate = {key: candidate.get(key) for key in ("role", "seat", "name")}
        return {"seq": row["seq"], "kind": "protocol", "op": row["op"],
                "created_at": _iso(row["created"]),
                "sender": {"role": row["sender_role"], "seat": row["sender_seat"],
                           "name": row["sender_name"]},
                "term": payload.get("term"), "candidate": candidate, "leader": leader}

    def _require_running(self, session):
        if session["status"] != "active":
            raise AuthError("团队会话未在进行，不能发起选举", 409)

    def _member_at(self, db, sid, role, seat):
        if role not in ("mentor", "worker"):
            raise ValueError("角色须为 mentor 或 worker")
        if isinstance(seat, bool) or not isinstance(seat, int):
            raise ValueError("seat必须是整数")
        row = db.execute("SELECT * FROM team_members WHERE session_id=? AND role=? AND seat=?",
                         (sid, role, seat)).fetchone()
        if row is None:
            raise ValueError("目标席位不存在")
        return row

    def _seat_brief(self, member):
        return {"role": member["role"], "seat": member["seat"], "name": member["name"]}

    def _ballot(self, db, session, member, now, *, elected_member, already_voted, retry_after=None):
        leader = self._leader_public(db, session, now)
        body = {"election": self._election_view(db, session, now),
                "session": self._session(db, session, member),
                "elected": leader if elected_member is not None else None,
                "already_voted": bool(already_voted)}
        if retry_after is not None:
            body["retry_after"] = retry_after
        return body

    def who_is_leader(self, actor, sid):
        self._touch(sid, _actor(actor))
        with self._read() as db:
            session, member = self._access(db, actor, sid)
            view = self._election_view(db, session)
            view["role"] = member["role"]
            view["seat"] = member["seat"]
            return view

    def list_online(self, actor, sid):
        self._touch(sid, _actor(actor))
        with self._read() as db:
            session, member = self._access(db, actor, sid)
            rows = db.execute("SELECT * FROM team_members WHERE session_id=? ORDER BY role, seat",
                              (sid,)).fetchall()
            roster = [self._roster_member(row, session) for row in rows]
            return {"members": roster, "online": [item for item in roster if item["online"]],
                    "live": [item for item in roster if item["live"]],
                    "leader": self._leader_public(db, session),
                    "role": member["role"], "seat": member["seat"]}

    def campaign(self, actor, sid):
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            self._require_running(session)
            if member["role"] not in ("mentor", "worker"):
                raise AuthError("只有指导端或执行端可以参选", 403)
            now = time.time()
            self._mark_seen(db, sid, member["actor_key"], now)
            if self._blocked_by_live_leader(db, session, now):
                raise AuthError("已有任期未结束的领导者", 409)
            session = self._load_session(db, sid)
            retry = self._stall_blocks(db, session, now)
            if retry is not None:
                session = self._load_session(db, sid)
                return self._ballot(db, session, member, now, elected_member=None,
                                    already_voted=False, retry_after=retry)
            term = self._open_election(db, session, now)
            changed = self._cast_vote(db, sid, term, member, member)
            elected = self._maybe_elect(db, sid, term, now)
            session = self._load_session(db, sid)
            if changed or elected is not None:
                self._append_protocol(db, sid, member, "elected" if elected is not None else "campaign", {
                    "op": "elected" if elected is not None else "campaign", "term": term,
                    "candidate": self._seat_brief(member),
                    "leader": self._leader_public(db, session, now)})
                session = self._load_session(db, sid)
            return self._ballot(db, session, member, now, elected_member=elected,
                                already_voted=not changed)

    def vote(self, actor, sid, body):
        body = _body(body, {"role", "seat", "term"})
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            self._require_running(session)
            if member["role"] not in ("mentor", "worker"):
                raise AuthError("只有指导端或执行端可以投票", 403)
            now = time.time()
            self._mark_seen(db, sid, member["actor_key"], now)
            if self._blocked_by_live_leader(db, session, now):
                raise AuthError("已有任期未结束的领导者", 409)
            session = self._load_session(db, sid)
            if not self._election_open(session, now):
                raise AuthError("没有进行中的选举", 409)
            term = int(session["leader_term"])
            requested = body.get("term")
            if requested is not None and (isinstance(requested, bool) or not isinstance(requested, int)
                                           or requested != term):
                raise AuthError("投票任期与当前选举不一致", 409)
            candidate = self._member_at(db, sid, body.get("role"), body.get("seat"))
            changed = self._cast_vote(db, sid, term, member, candidate)
            elected = self._maybe_elect(db, sid, term, now)
            session = self._load_session(db, sid)
            if changed or elected is not None:
                won = elected is not None
                self._append_protocol(db, sid, member, "elected" if won else "vote", {
                    "op": "elected" if won else "vote", "term": term,
                    "candidate": self._seat_brief(candidate),
                    "leader": self._leader_public(db, session, now)})
                session = self._load_session(db, sid)
            return self._ballot(db, session, member, now, elected_member=elected,
                                already_voted=not changed)

    def appoint(self, actor, sid, body):
        body = _body(body, {"role", "seat"})
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            self._require_running(session)
            if member["role"] not in ("owner", "mentor") and not self._is_leader_member(session, member):
                raise AuthError("只有创建者、指导端或现任领导者可以指定领导者", 403)
            target = self._member_at(db, sid, body.get("role"), body.get("seat"))
            now = time.time()
            term = int(session["leader_term"] or 0) + 1
            db.execute("DELETE FROM team_votes WHERE session_id=?", (sid,))
            self._set_leader(db, sid, target, term, now, (1, 1, 1))
            session = self._load_session(db, sid)
            self._append_protocol(db, sid, member, "appoint", {
                "op": "appoint", "term": term, "candidate": self._seat_brief(target),
                "leader": self._leader_public(db, session, now)})
            session = self._load_session(db, sid)
            leader = self._leader_public(db, session, now)
            return {"leader": leader, "elected": leader,
                    "election": self._election_view(db, session, now),
                    "session": self._session(db, session, member)}

    def leader_heartbeat(self, actor, sid):
        with self._write(notify=False) as db:
            session, member = self._access(db, actor, sid)
            now = time.time()
            if not self._is_leader_member(session, member, now):
                raise AuthError("只有现任领导者可以续约", 409)
            db.execute("UPDATE team_sessions SET leader_lease=?, updated=? WHERE id=?",
                       (now + self.LEASE_SECONDS, now, sid))
            db.execute("UPDATE team_members SET last_seen=? WHERE session_id=? AND actor_key=?",
                       (now, sid, member["actor_key"]))
            session = self._load_session(db, sid)
            return {"leader": self._leader_public(db, session, now),
                    "session": self._session(db, session, member)}

    def _parse_protocol(self, text):
        body = text.strip()
        pieces = body.split(None, 1)
        op = pieces[0]
        raw = pieces[1].strip() if len(pieces) == 2 else ""
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ValueError("协议备注的JSON不合法") from error
            if not isinstance(data, dict) or set(data) - {"term", "candidate", "role", "seat"}:
                raise ValueError("协议备注字段不合法")
            term = data.get("term")
            role = seat = None
            if "candidate" in data:
                role, seat = self._split_target(data.get("candidate"))
            elif "role" in data or "seat" in data:
                role, seat = data.get("role"), data.get("seat")
            self._check_term(term)
            self._check_target(role, seat, allow_empty=op != "VOTE")
            return {"op": op, "term": term, "role": role, "seat": seat}
        rest = raw.split() if raw else []
        term = None
        if rest and rest[0].isdigit():
            term = int(rest[0])
            rest = rest[1:]
        self._check_term(term)
        if op == "HB":
            return {"op": op, "term": term, "role": None, "seat": None}
        role = seat = None
        if rest:
            if len(rest) != 1:
                raise ValueError("协议备注格式不合法")
            role, seat = self._split_target(rest[0])
        if op == "VOTE" and role is None:
            raise ValueError("VOTE必须带候选席位")
        self._check_target(role, seat, allow_empty=op != "VOTE")
        return {"op": op, "term": term, "role": role, "seat": seat}

    @staticmethod
    def _check_term(term):
        if term is None:
            return
        if isinstance(term, bool) or not isinstance(term, int) or term < 0 or term > 1_000_000:
            raise ValueError("term不合法")

    @staticmethod
    def _split_target(value):
        if isinstance(value, str) and value.isdigit():
            return "worker", int(value)
        if not isinstance(value, str) or value.count(":") != 1:
            raise ValueError("候选席位须为 mentor:0 或 worker:N")
        role, seat = value.split(":", 1)
        if role not in ("mentor", "worker") or not seat.isdigit():
            raise ValueError("候选席位须为 mentor:0 或 worker:N")
        return role, int(seat)

    @staticmethod
    def _check_target(role, seat, allow_empty):
        if role is None and seat is None:
            if allow_empty:
                return
            raise ValueError("候选席位不能为空")
        if role not in ("mentor", "worker") or isinstance(seat, bool) or not isinstance(seat, int):
            raise ValueError("候选席位须为 mentor:0 或 worker:N")

    def _note_outcome(self, applied, reason=None, elected=None, already_voted=False, retry_after=None):
        return {"applied": applied, "reason": reason, "elected": elected,
                "already_voted": already_voted, "retry_after": retry_after}

    def _protocol_result(self, db, session, member, op, applied, duplicate, reason,
                         elected=None, already_voted=False, retry_after=None):
        payload = {"protocol": True, "op": op, "applied": applied, "duplicate": duplicate,
                   "leader": self._leader_public(db, session),
                   "elected": self._leader_public(db, session) if elected is not None else None,
                   "already_voted": bool(already_voted),
                   "election": self._election_view(db, session),
                   "session": self._session(db, session, member)}
        if reason:
            payload["reason"] = reason
        if retry_after is not None:
            payload["retry_after"] = retry_after
        return payload

    def _apply_protocol_note(self, db, session, member, parsed):
        op = parsed["op"]
        now = time.time()
        sid = session["id"]
        self._mark_seen(db, sid, member["actor_key"], now)
        if op == "HB":
            if self._is_leader_member(session, member, now):
                db.execute("UPDATE team_sessions SET leader_lease=? WHERE id=?",
                           (now + self.LEASE_SECONDS, sid))
            return self._note_outcome(True)
        if op == "LEADER":
            if self._is_leader_member(session, member, now):
                db.execute("UPDATE team_sessions SET leader_lease=? WHERE id=?",
                           (now + self.LEASE_SECONDS, sid))
                return self._note_outcome(True)
            return self._note_outcome(False, "not_leader")
        if member["role"] not in ("mentor", "worker"):
            return self._note_outcome(False, "not_eligible")
        if session["status"] != "active":
            return self._note_outcome(False, "not_active")
        if self._blocked_by_live_leader(db, session, now):
            return self._note_outcome(False, "leader_active")
        session = self._load_session(db, sid)
        if op == "ELECT":
            target = member
            if parsed["role"] is not None:
                target = db.execute("SELECT * FROM team_members WHERE session_id=? AND role=? AND seat=?",
                                    (sid, parsed["role"], parsed["seat"])).fetchone()
                if target is None or target["role"] not in ("mentor", "worker"):
                    return self._note_outcome(False, "unknown_seat")
            retry = self._stall_blocks(db, session, now)
            if retry is not None:
                return self._note_outcome(False, "election_backoff", retry_after=retry)
            term = self._open_election(db, session, now)
            changed = self._cast_vote(db, sid, term, member, target)
            elected = self._maybe_elect(db, sid, term, now)
            if changed or elected is not None:
                session = self._load_session(db, sid)
                won = elected is not None
                self._append_protocol(db, sid, member, "elected" if won else "campaign", {
                    "op": "elected" if won else "campaign", "term": term,
                    "candidate": self._seat_brief(target),
                    "leader": self._leader_public(db, session, now)})
            return self._note_outcome(True, elected=elected, already_voted=not changed)
        if op == "VOTE":
            if not self._election_open(session, now):
                return self._note_outcome(False, "no_election")
            target = db.execute("SELECT * FROM team_members WHERE session_id=? AND role=? AND seat=?",
                                (sid, parsed["role"], parsed["seat"])).fetchone()
            if target is None:
                return self._note_outcome(False, "unknown_seat")
            term = int(session["leader_term"])
            changed = self._cast_vote(db, sid, term, member, target)
            elected = self._maybe_elect(db, sid, term, now)
            if changed or elected is not None:
                session = self._load_session(db, sid)
                won = elected is not None
                self._append_protocol(db, sid, member, "elected" if won else "vote", {
                    "op": "elected" if won else "vote", "term": term,
                    "candidate": self._seat_brief(target),
                    "leader": self._leader_public(db, session, now)})
            return self._note_outcome(True, elected=elected, already_voted=not changed)
        return self._note_outcome(False, "unknown_op")

    def _ingest_protocol_note(self, actor, sid, text, request_id):
        parsed = self._parse_protocol(text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            if session["status"] == "closed":
                raise AuthError("团队会话已结束，不能发送新消息", 409)
            previous = db.execute("SELECT op, content_hash FROM team_protocol_requests "
                                  "WHERE session_id=? AND sender_key=? AND request_id=?",
                                  (sid, member["actor_key"], request_id)).fetchone()
            if previous is not None:
                if previous["content_hash"] != digest:
                    raise AuthError("相同request_id不能对应不同消息", 409)
                return self._protocol_result(db, session, member, previous["op"], True, True, None)
            outcome = self._apply_protocol_note(db, session, member, parsed)
            db.execute("INSERT INTO team_protocol_requests VALUES (?,?,?,?,?)",
                       (sid, member["actor_key"], request_id, digest, parsed["op"]))
            db.execute("UPDATE team_members SET last_seen=? WHERE session_id=? AND actor_key=?",
                       (time.time(), sid, member["actor_key"]))
            session = self._load_session(db, sid)
            return self._protocol_result(
                db, session, member, parsed["op"], outcome["applied"], False, outcome["reason"],
                elected=outcome["elected"], already_voted=outcome["already_voted"],
                retry_after=outcome["retry_after"])

    @staticmethod
    def _protocol_note_row(row):
        if row["kind"] != "message":
            return False
        try:
            message = json.loads(row["payload"]).get("message") or {}
        except json.JSONDecodeError:
            return False
        return protocol_op(message.get("text")) is not None

    def _work_preview(self, db, session_id):
        rows = db.execute(
            "SELECT kind, created, payload FROM team_events WHERE session_id=? "
            "AND kind IN ('message','task') ORDER BY seq DESC LIMIT 40",
            (session_id,)).fetchall()
        for row in rows:
            if row["kind"] == "task":
                task = json.loads(row["payload"]).get("task") or {}
                title = task.get("title") if isinstance(task.get("title"), str) else ""
                return {"kind": "task", "created_at": _iso(row["created"]), "preview": _preview(title)}
            message = json.loads(row["payload"]).get("message") or {}
            if protocol_op(message.get("text")):
                continue
            kind = message.get("kind") if message.get("kind") in (
                "instruction", "progress", "result", "question", "note") else None
            text = message.get("text") if isinstance(message.get("text"), str) else ""
            return {"kind": kind, "created_at": _iso(row["created"]), "preview": _preview(text)}
        return None

    def _filtered_public_event(self, db, row, public_id=None):
        if row["kind"] == "environment" or self._protocol_note_row(row):
            return None
        event = {"seq": row["seq"], "kind": row["kind"], "created_at": _iso(row["created"]),
                 "sender": {"role": row["sender_role"], "seat": row["sender_seat"],
                            "name": row["sender_name"]}}
        if row["kind"] == "task":
            payload = json.loads(row["payload"])
            task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
            event["action"] = payload.get("action") if isinstance(payload.get("action"), str) else None
            event["task"] = self._public_task(task)
            return event
        event.update(Relay._public_payload(
            row["kind"], json.loads(row["payload"]), db, row["session_id"], "team_files",
            public_id=public_id, file_base=self._base_url() if public_id else ""))
        return event

    def _task_rows(self, db, sid, status=None):
        if status is None:
            return db.execute("SELECT * FROM team_tasks WHERE session_id=? ORDER BY created ASC LIMIT ?",
                              (sid, self.MAX_TASKS)).fetchall()
        return db.execute("SELECT * FROM team_tasks WHERE session_id=? AND status=? "
                          "ORDER BY created ASC LIMIT ?",
                          (sid, status, self.MAX_TASKS)).fetchall()

    def _task_row(self, db, sid, task_id):
        if not isinstance(task_id, str) or not task_id.startswith("tt_"):
            raise ValueError("task_id不合法")
        row = db.execute("SELECT * FROM team_tasks WHERE id=? AND session_id=?",
                         (task_id, sid)).fetchone()
        if row is None:
            raise AuthError("没有这个任务", 404)
        return row

    @staticmethod
    def _task_json(row):
        assignee = None
        if row["assignee_role"]:
            assignee = {"role": row["assignee_role"], "seat": row["assignee_seat"],
                        "name": row["assignee_name"]}
        return {"id": row["id"], "title": row["title"], "detail": row["detail"],
                "status": row["status"], "progress": row["progress"], "assignee": assignee,
                "parent_id": row["parent_id"],
                "created_by": {"role": row["created_by_role"], "seat": row["created_by_seat"],
                               "name": row["created_by_name"]},
                "created_at": _iso(row["created"]), "updated_at": _iso(row["updated"]),
                "revision": row["revision"]}

    @staticmethod
    def _public_task(task):
        assignee = task.get("assignee") if isinstance(task.get("assignee"), dict) else None
        created_by = task.get("created_by") if isinstance(task.get("created_by"), dict) else None
        if assignee is not None:
            assignee = {"role": assignee.get("role"), "seat": assignee.get("seat"),
                        "name": assignee.get("name")}
        if created_by is not None:
            created_by = {"role": created_by.get("role"), "seat": created_by.get("seat"),
                          "name": created_by.get("name")}
        return {"id": task.get("id"), "title": task.get("title"), "detail": task.get("detail"),
                "status": task.get("status"), "progress": task.get("progress"),
                "assignee": assignee, "parent_id": task.get("parent_id"),
                "created_by": created_by, "created_at": task.get("created_at"),
                "updated_at": task.get("updated_at"), "revision": task.get("revision")}

    @staticmethod
    def _check_progress(value):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise ValueError("progress须为0到100的整数")
        return value

    def _require_task_open_session(self, session):
        if session["status"] == "closed":
            raise AuthError("团队会话已结束", 409)

    def list_tasks(self, actor, sid, status=None):
        if status is not None and status not in ("open", "in_progress", "done"):
            raise ValueError("status须为 open、in_progress 或 done")
        self._touch(sid, _actor(actor))
        with self._read() as db:
            session, _member = self._access(db, actor, sid)
            return {"tasks": [self._task_json(row) for row in self._task_rows(db, sid, status)],
                    "leader": self._leader_public(db, session)}

    def create_task(self, actor, sid, body):
        body = _body(body, {"title", "detail", "request_id", "parent_id"})
        title = _text(body.get("title"), "标题", 160)
        detail = body.get("detail") or ""
        if not isinstance(detail, str) or len(detail) > 8000 or "\x00" in detail:
            raise ValueError("任务说明不能超过长度限制")
        detail = detail.strip()
        request_id = _text(body.get("request_id"), "request_id", 160)
        parent_id = body.get("parent_id")
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id.startswith("tt_")):
            raise ValueError("parent_id不合法")
        digest = hashlib.sha256(json.dumps(
            {"title": title, "detail": detail, "parent_id": parent_id},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            self._require_task_open_session(session)
            existing = db.execute("SELECT * FROM team_tasks WHERE session_id=? AND creator_key=? AND request_id=?",
                                  (sid, member["actor_key"], request_id)).fetchone()
            if existing is not None:
                if existing["content_hash"] != digest:
                    raise AuthError("相同request_id不能对应不同任务", 409)
                return {"task": self._task_json(existing), "duplicate": True}
            if parent_id and not db.execute("SELECT 1 FROM team_tasks WHERE id=? AND session_id=?",
                                            (parent_id, sid)).fetchone():
                raise ValueError("父任务不属于本团队会话")
            now = time.time()
            task_id = "tt_" + secrets.token_hex(8)
            db.execute("INSERT INTO team_tasks (id,session_id,title,detail,status,assignee_role,"
                       "assignee_seat,assignee_name,assignee_key,progress,parent_id,created_by_role,"
                       "created_by_seat,created_by_name,creator_key,request_id,content_hash,created,"
                       "updated,revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (task_id, sid, title, detail, "open", None, None, None, None, 0, parent_id,
                        member["role"], member["seat"], member["name"], member["actor_key"],
                        request_id, digest, now, now, 1))
            row = db.execute("SELECT * FROM team_tasks WHERE id=?", (task_id,)).fetchone()
            task = self._task_json(row)
            self._append(db, sid, member, "task", {"action": "created", "task": task})
            return {"task": task, "duplicate": False}

    def claim_task(self, actor, sid, task_id):
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            self._require_task_open_session(session)
            if member["role"] not in ("mentor", "worker"):
                raise AuthError("只有指导端或执行端可以认领任务", 403)
            row = self._task_row(db, sid, task_id)
            if row["status"] == "done":
                raise AuthError("任务已完成", 409)
            if row["assignee_key"] and row["assignee_key"] != member["actor_key"]:
                raise AuthError("任务已被其他人认领", 409)
            if row["assignee_key"] == member["actor_key"] and row["status"] == "in_progress":
                return {"task": self._task_json(row), "duplicate": True}
            now = time.time()
            db.execute("UPDATE team_tasks SET status='in_progress', assignee_role=?, assignee_seat=?, "
                       "assignee_name=?, assignee_key=?, updated=?, revision=revision+1 WHERE id=?",
                       (member["role"], member["seat"], member["name"], member["actor_key"], now, row["id"]))
            row = db.execute("SELECT * FROM team_tasks WHERE id=?", (row["id"],)).fetchone()
            task = self._task_json(row)
            self._append(db, sid, member, "task", {"action": "claimed", "task": task})
            return {"task": task, "duplicate": False}

    def assign_task(self, actor, sid, task_id, body):
        body = _body(body, {"role", "seat", "task_id", "allow_offline"})
        allow_offline = body.get("allow_offline", False)
        if not isinstance(allow_offline, bool):
            raise ValueError("allow_offline须为布尔值")
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            self._require_task_open_session(session)
            if member["role"] not in ("owner", "mentor") and not self._is_leader_member(session, member):
                raise AuthError("只有创建者、指导端或现任领导者可以指派任务", 403)
            target = self._member_at(db, sid, body.get("role"), body.get("seat"))
            row = self._task_row(db, sid, task_id)
            if row["status"] == "done":
                raise AuthError("任务已完成", 409)
            if row["assignee_key"] == target["actor_key"] and row["status"] == "in_progress":
                return {"task": self._task_json(row), "duplicate": True}
            now = time.time()
            warning = None
            if not self._member_live(target, session, now):
                if not allow_offline:
                    raise AuthError("目标席位已离开租约窗口。确认后可带 allow_offline=true 再指派", 409)
                warning = "目标席位已离开租约窗口，仍已指派"
            db.execute("UPDATE team_tasks SET status='in_progress', assignee_role=?, assignee_seat=?, "
                       "assignee_name=?, assignee_key=?, updated=?, revision=revision+1 WHERE id=?",
                       (target["role"], target["seat"], target["name"], target["actor_key"], now, row["id"]))
            row = db.execute("SELECT * FROM team_tasks WHERE id=?", (row["id"],)).fetchone()
            task = self._task_json(row)
            self._append(db, sid, member, "task", {"action": "assigned", "task": task})
            result = {"task": task, "duplicate": False}
            if warning:
                result["warning"] = warning
            return result

    def update_task(self, actor, sid, task_id, body):
        body = _body(body, {"progress", "status", "detail", "task_id"})
        if not any(key in body for key in ("progress", "status", "detail")):
            raise ValueError("需要 progress、status 或 detail")
        progress = self._check_progress(body["progress"]) if "progress" in body else None
        status = body.get("status") if "status" in body else None
        if status is not None and status not in ("open", "in_progress", "done"):
            raise ValueError("status须为 open、in_progress 或 done")
        detail = None
        if "detail" in body:
            detail = body.get("detail")
            if not isinstance(detail, str) or len(detail) > 8000 or "\x00" in detail:
                raise ValueError("任务说明不能超过长度限制")
            detail = detail.strip()
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            self._require_task_open_session(session)
            row = self._task_row(db, sid, task_id)
            staff = member["role"] in ("owner", "mentor") or self._is_leader_member(session, member)
            if not staff and row["assignee_key"] != member["actor_key"]:
                raise AuthError("只有认领者、领导者或创建者可以更新任务", 403)
            new_status = row["status"] if status is None else status
            new_progress = row["progress"] if progress is None else progress
            new_detail = row["detail"] if detail is None else detail
            assignee_role, assignee_seat = row["assignee_role"], row["assignee_seat"]
            assignee_name, assignee_key = row["assignee_name"], row["assignee_key"]
            if new_status == "open":
                if not staff:
                    raise AuthError("只有创建者、指导端或领导者可以把任务退回待认领", 403)
                assignee_role = assignee_seat = assignee_name = assignee_key = None
            elif new_status == "in_progress" and not assignee_key:
                raise AuthError("进行中的任务需要认领者", 409)
            elif new_status == "done" and progress is None:
                new_progress = 100
            unchanged = (new_status == row["status"] and new_progress == row["progress"]
                         and new_detail == row["detail"] and assignee_key == row["assignee_key"])
            if unchanged:
                return {"task": self._task_json(row), "duplicate": True}
            progress_only = (new_status == row["status"] and new_detail == row["detail"]
                             and assignee_key == row["assignee_key"])
            now = time.time()
            db.execute("UPDATE team_tasks SET status=?, progress=?, detail=?, assignee_role=?, "
                       "assignee_seat=?, assignee_name=?, assignee_key=?, updated=?, revision=revision+1 "
                       "WHERE id=?",
                       (new_status, new_progress, new_detail, assignee_role, assignee_seat,
                        assignee_name, assignee_key, now, row["id"]))
            row = db.execute("SELECT * FROM team_tasks WHERE id=?", (row["id"],)).fetchone()
            task = self._task_json(row)
            # Pure progress ticks stay on the task row. Claim, assign, status,
            # and detail changes still append a work-stream event.
            if progress_only:
                db.execute("UPDATE team_members SET last_seen=? WHERE session_id=? AND actor_key=?",
                           (now, sid, member["actor_key"]))
                return {"task": task, "duplicate": False, "coalesced": True}
            self._append(db, sid, member, "task", {"action": "updated", "task": task})
            return {"task": task, "duplicate": False, "coalesced": False}
