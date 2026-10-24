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
    _actor, _body, _claim_waiter, _environment, _iso, _long_poll_ready, _message_preview,
    _mint_public_id, _name, _preview, _resolve_waiter_limits, _text, directory_rows,
    ensure_directory_columns,
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
    TOUCH_SECONDS = 60
    SIGNED_SECONDS = relay_files.SIGNED_SECONDS
    INVITE_SECONDS = 24 * 3600
    MAX_FILE_BYTES = 100 * 1024 * 1024
    MAX_ATTACHMENTS = 20

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
        self.MAX_WAITERS, self.MAX_SESSION_WAITERS = _resolve_waiter_limits()
        with self._write() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS team_sessions (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL,
                  status TEXT NOT NULL, owner_key TEXT NOT NULL,
                  created REAL NOT NULL, updated REAL NOT NULL,
                  last_seq INTEGER NOT NULL DEFAULT 0,
                  public_read INTEGER NOT NULL DEFAULT 0, public_token_hash TEXT,
                  visibility TEXT NOT NULL DEFAULT 'public', public_id TEXT);
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
            """)
            # Additive migration for databases created before public watch existed.
            columns = {row[1] for row in db.execute("PRAGMA table_info(team_sessions)")}
            if "public_read" not in columns:
                db.execute("ALTER TABLE team_sessions ADD COLUMN public_read "
                           "INTEGER NOT NULL DEFAULT 0")
            if "public_token_hash" not in columns:
                db.execute("ALTER TABLE team_sessions ADD COLUMN public_token_hash TEXT")
            ensure_directory_columns(db, "team_sessions")

    # -- plumbing -------------------------------------------------------

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
        now = time.time()
        with self._write(notify=False) as db:
            row = db.execute("SELECT last_seen FROM team_members WHERE session_id=? AND actor_key=?",
                             (sid, key)).fetchone()
            if row is not None and now - row["last_seen"] >= self.TOUCH_SECONDS:
                db.execute("UPDATE team_members SET last_seen=? WHERE session_id=? AND actor_key=?",
                           (now, sid, key))

    def _peer(self, row, include_environment=True):
        item = {"role": row["role"], "seat": row["seat"], "name": row["name"],
                "online": time.time() - row["last_seen"] < self.ONLINE_SECONDS,
                "last_seen": _iso(row["last_seen"]), "ack_seq": row["ack_seq"]}
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
        return {"id": session["id"], "title": session["title"], "goal": session["goal"],
                "status": session["status"], "created_at": _iso(session["created"]),
                "updated_at": _iso(session["updated"]), "last_seq": session["last_seq"],
                "owner": member["role"] == "owner", "owner_name": owner["name"],
                "role": member["role"], "seat": member["seat"],
                "public_read": bool(session["public_read"]),
                "visibility": session["visibility"] or "public",
                "public_id": session["public_id"],
                "max_workers": self.max_workers,
                "mentor": self._peer(mentor) if mentor else None,
                "workers": [self._peer(row, member["role"] != "worker"
                                       or row["seat"] == member["seat"])
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
            if session is None or session["status"] == "closed":
                raise AuthError("团队会话已结束，不能加入", 409)
            if db.execute("SELECT 1 FROM team_revoked WHERE session_id=? AND actor_key=?",
                          (session["id"], key)).fetchone():
                raise AuthError("该身份已被撤销，不能重新加入团队会话", 403)
            if db.execute("SELECT 1 FROM team_members WHERE session_id=? AND actor_key=?",
                          (session["id"], key)).fetchone():
                raise AuthError("该身份已参与团队会话", 409)
            if invite["role"] == "mentor":
                if "environment" in body:
                    raise AuthError("只有执行端可以上报系统环境", 403)
                if db.execute("SELECT 1 FROM team_members WHERE session_id=? AND role='mentor'",
                              (session["id"],)).fetchone():
                    raise AuthError("指导端席位已被占用", 409)
                seat, environment = 0, None
            else:
                if not body.get("environment"):
                    raise ValueError("执行端加入必须上报 environment"
                                     "（os_name、os_version、architecture）")
                environment = _environment(body.get("environment"))
                if invite["seat"] is not None:
                    if db.execute("SELECT 1 FROM team_members WHERE session_id=? AND role='worker' "
                                  "AND seat=?", (session["id"], invite["seat"])).fetchone():
                        raise AuthError("该席位已被占用", 409)
                    seat = invite["seat"]
                else:
                    used = {row[0] for row in db.execute(
                        "SELECT seat FROM team_members WHERE session_id=? AND role='worker'",
                        (session["id"],))}
                    seat = next((index for index in range(1, self.max_workers + 1)
                                 if index not in used), None)
                    if seat is None:
                        raise AuthError("执行端席位已满", 409)
            now = time.time()
            db.execute("INSERT INTO team_members VALUES (?,?,?,?,?,?,?,?)",
                       (session["id"], invite["role"], seat, key, _name(actor), now, 0,
                        json.dumps(environment, sort_keys=True, ensure_ascii=False)
                        if environment else None))
            db.execute("UPDATE team_invites SET used_at=?, used_by_key=?, used_by_name=? WHERE id=?",
                       (now, key, _name(actor), invite["id"]))
            member = db.execute("SELECT * FROM team_members WHERE session_id=? AND actor_key=?",
                                (session["id"], key)).fetchone()
            self._append(db, session["id"], member, "joined",
                         {"role": invite["role"], "seat": seat})
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
                "mentor": ({"role": "mentor", "seat": 0, "name": mentor["name"]}
                           if mentor else None),
                "workers": workers}

    def _public_event(self, db, row):
        if row["kind"] == "environment":
            return None
        event = {"seq": row["seq"], "kind": row["kind"], "created_at": _iso(row["created"]),
                 "sender": {"role": row["sender_role"], "seat": row["sender_seat"],
                            "name": row["sender_name"]}}
        event.update(Relay._public_payload(row["kind"], json.loads(row["payload"]),
                                           db, row["session_id"], "team_files"))
        return event

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
        return {"public_id": session["public_id"], "mode": "team", "title": session["title"],
                "goal": _preview(goal) if summary else goal, "status": session["status"],
                "created_at": _iso(session["created"]), "updated_at": _iso(session["updated"]),
                "participants": participants,
                "last_message": _message_preview(db, session["id"], "team_events")}

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
        if row["kind"] == "environment":
            return None
        event = {"seq": row["seq"], "kind": row["kind"], "created_at": _iso(row["created"]),
                 "sender": {"role": row["sender_role"], "seat": row["sender_seat"],
                            "name": row["sender_name"]}}
        event.update(Relay._public_payload(row["kind"], json.loads(row["payload"]),
                                           db, row["session_id"], "team_files",
                                           public_id=public_id, file_base=self._base_url()))
        return event

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

    def _visible(self, member, row):
        if member["role"] in ("owner", "mentor"):
            return True
        if row["kind"] != "message":
            return True
        message = (json.loads(row["payload"]).get("message") or {})
        to = message.get("to")
        if to == "all":
            return True
        if isinstance(to, str) and to.startswith("worker:"):
            target = int(to.split(":", 1)[1])
            return target == member["seat"] or row["sender_seat"] == member["seat"]
        if message.get("kind") == "instruction":
            return True
        if row["sender_role"] == "worker" and message.get("kind") in ("result", "progress", "question", "note"):
            return row["sender_seat"] == member["seat"]
        return True

    def _page(self, actor, sid, after):
        with self._read() as db:
            session, member = self._access(db, actor, sid)
            if after > session["last_seq"]:
                raise ValueError("消息游标超过当前会话进度")
            rows = db.execute("SELECT * FROM team_events WHERE session_id=? AND seq>? "
                              "ORDER BY seq LIMIT ?", (sid, after, self.PAGE_SIZE + 1)).fetchall()
            scanned = rows[:self.PAGE_SIZE]
            visible = [row for row in scanned if self._visible(member, row)]
            return {"session": self._session(db, session, member),
                    "events": [self._event(db, row) for row in visible],
                    "next_cursor": scanned[-1]["seq"] if scanned else after,
                    "has_more": len(rows) > self.PAGE_SIZE}

    def events(self, actor, sid, after=0, wait=0, abandon=None):
        key = _actor(actor)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after须为非负整数")
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or not 0 <= wait <= 20:
            raise ValueError("wait须为0至20秒")
        waiter = (sid, key)
        registered = False
        deadline = time.monotonic() + wait
        try:
            with self._condition:
                while True:
                    result = self._page(actor, sid, after)
                    if _long_poll_ready(result, wait, deadline, abandon):
                        self._touch(sid, key)
                        with self._read() as db:
                            result["session"] = self._view(db, actor, sid)
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

    def send(self, actor, sid, body):
        body = _body(body, {"kind", "text", "request_id", "reply_to", "to", "attachments"})
        kind = body.get("kind")
        if kind not in ("instruction", "progress", "result", "question", "note"):
            raise ValueError("未知消息类型")
        text = _text(body.get("text"), "消息", 8000)
        request_id = _text(body.get("request_id"), "request_id", 160)
        reply = body.get("reply_to")
        if reply is not None and (isinstance(reply, bool) or not isinstance(reply, int) or reply < 1):
            raise ValueError("reply_to须为本会话消息序号")
        to = body.get("to")
        if to is not None:
            if not isinstance(to, str) or not (to == "all" or to.startswith("worker:")):
                raise ValueError('to 必须是 "all" 或 "worker:N"')
            if to != "all":
                suffix = to.split(":", 1)[1]
                if not suffix.isdigit() or not 1 <= int(suffix) <= self.max_workers:
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
            if kind == "instruction" and member["role"] not in ("mentor", "owner"):
                raise AuthError("只有指挥端或管理者可以发送指令", 403)
            if kind in ("progress", "result") and member["role"] != "worker":
                raise AuthError("只有执行端可以回报进度或结果", 403)
            if kind == "instruction" and session["status"] == "paused":
                raise AuthError("团队会话已暂停，不能发送新指令", 409)
            if reply is not None and (reply > session["last_seq"] or not db.execute(
                    "SELECT 1 FROM team_events WHERE session_id=? AND seq=?", (sid, reply)).fetchone()):
                raise ValueError("reply_to不属于本团队会话历史")
            if to and to.startswith("worker:") and member["role"] not in ("mentor", "owner"):
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
            if kind == "result" and reply is not None:
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
            db.execute("UPDATE team_members SET last_seen=? WHERE session_id=? AND actor_key=?",
                       (time.time(), sid, member["actor_key"]))
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
                if not 1 <= seat <= self.max_workers:
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
                    if not 1 <= seat <= self.max_workers:
                        raise ValueError("worker 席位超出范围")
                    if db.execute("SELECT 1 FROM team_members WHERE session_id=? AND role='worker' "
                                  "AND seat=?", (sid, seat)).fetchone():
                        raise AuthError("该席位已被占用", 409)
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
