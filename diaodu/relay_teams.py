"""One-to-many relay teams.

Deliberately separate from the one-to-one relay: its own SQLite file, tables,
routes (added in a later batch) and tool group. Nothing here touches
relay.sqlite3 or the existing `/v1/relay/sessions/...` code.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from diaodu.identity import AuthError
from diaodu.relay import _actor, _body, _environment, _iso, _name, _text


class RelayTeams:
    MAX_WORKERS_CAP = 8
    MAX_EVENTS = 5000
    PAGE_SIZE = 100
    MAX_WAITERS = 32
    ONLINE_SECONDS = 60
    TOUCH_SECONDS = 60

    def __init__(self, root: Path, max_workers: int = 8):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        try:
            self.max_workers = max(1, min(int(max_workers), self.MAX_WORKERS_CAP))
        except (TypeError, ValueError):
            self.max_workers = self.MAX_WORKERS_CAP
        self.path = root / "relay_teams.sqlite3"
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        self._condition = threading.Condition(threading.RLock())
        self._waiters = set()
        with self._write() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS team_sessions (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL,
                  status TEXT NOT NULL, owner_key TEXT NOT NULL,
                  created REAL NOT NULL, updated REAL NOT NULL,
                  last_seq INTEGER NOT NULL DEFAULT 0, topic_code_hash TEXT);
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
            """)

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

    def _peer(self, row):
        item = {"role": row["role"], "seat": row["seat"], "name": row["name"],
                "online": time.time() - row["last_seen"] < self.ONLINE_SECONDS,
                "last_seen": _iso(row["last_seen"]), "ack_seq": row["ack_seq"]}
        if row["role"] == "worker":
            item["environment"] = json.loads(row["environment"]) if row["environment"] else None
        return item

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
                "mentor": self._peer(mentor) if mentor else None,
                "workers": [self._peer(row) for row in workers]}

    def _event(self, db, row):
        result = {"seq": row["seq"], "kind": row["kind"], "created_at": _iso(row["created"]),
                  "sender": {"role": row["sender_role"], "seat": row["sender_seat"],
                             "name": row["sender_name"]}}
        result.update(json.loads(row["payload"]))
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
        topic_code = secrets.token_urlsafe(24)
        with self._write() as db:
            db.execute("INSERT INTO team_sessions VALUES (?,?,?,?,?,?,?,?,?)",
                       (sid, title, goal, "active", key, now, now, 0,
                        hashlib.sha256(topic_code.encode()).hexdigest()))
            db.execute("INSERT INTO team_members VALUES (?,?,?,?,?,?,?,?)",
                       (sid, "owner", 0, key, _name(actor), now, 0, None))
            return {"session": self._session(db, db.execute(
                "SELECT * FROM team_sessions WHERE id=?", (sid,)).fetchone(),
                db.execute("SELECT * FROM team_members WHERE session_id=? AND actor_key=?",
                           (sid, key)).fetchone()),
                "topic_code": topic_code}

    def join(self, actor, body):
        key = _actor(actor)
        body = _body(body, {"code", "role", "environment"})
        code = _text(body.get("code"), "配对码", 160)
        hashed = hashlib.sha256(code.encode()).hexdigest()
        role = body.get("role")
        if role not in ("mentor", "worker"):
            raise ValueError("使用团队配对码时必须选择 mentor 或 worker 角色")
        with self._write() as db:
            session = db.execute("SELECT * FROM team_sessions WHERE topic_code_hash=?",
                                 (hashed,)).fetchone()
            if session is None or session["status"] == "closed":
                raise AuthError("团队配对码无效或已失效", 403)
            if db.execute("SELECT 1 FROM team_members WHERE session_id=? AND actor_key=?",
                          (session["id"], key)).fetchone():
                raise AuthError("该身份已参与团队会话", 409)
            if role == "mentor":
                if "environment" in body:
                    raise AuthError("只有执行端可以上报系统环境", 403)
                seat = 0
                if db.execute("SELECT 1 FROM team_members WHERE session_id=? AND role='mentor'",
                              (session["id"],)).fetchone():
                    raise AuthError("指导端席位已被占用", 409)
                environment = None
            else:
                environment = _environment(body.get("environment"))
                used = {row[0] for row in db.execute(
                    "SELECT seat FROM team_members WHERE session_id=? AND role='worker'",
                    (session["id"],))}
                seat = next((index for index in range(1, self.max_workers + 1) if index not in used), None)
                if seat is None:
                    raise AuthError("执行端席位已满", 409)
            now = time.time()
            db.execute("INSERT INTO team_members VALUES (?,?,?,?,?,?,?,?)",
                       (session["id"], role, seat, key, _name(actor), now, 0,
                        json.dumps(environment, sort_keys=True, ensure_ascii=False)
                        if environment else None))
            member = db.execute("SELECT * FROM team_members WHERE session_id=? AND actor_key=?",
                                (session["id"], key)).fetchone()
            self._append(db, session["id"], member, "joined", {"role": role, "seat": seat})
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
        if row["sender_role"] == "worker" and message.get("kind") in ("result", "progress", "question"):
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

    def events(self, actor, sid, after=0, wait=0):
        key = _actor(actor)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after须为非负整数")
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or not 0 <= wait <= 20:
            raise ValueError("wait须为0至20秒")
        token = (sid, key)
        registered = False
        deadline = time.monotonic() + wait
        try:
            with self._condition:
                while True:
                    result = self._page(actor, sid, after)
                    if result["events"] or not wait or result["session"]["status"] == "closed" or time.monotonic() >= deadline:
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
                        if token in self._waiters or len(self._waiters) >= self.MAX_WAITERS:
                            raise AuthError("实时等待连接已达上限，请稍后重试", 429)
                        self._waiters.add(token)
                        registered = True
                    self._condition.wait(min(1.0, max(0, deadline - time.monotonic())))
        finally:
            if registered:
                with self._condition:
                    self._waiters.discard(token)

    def _view(self, db, actor, sid):
        session, member = self._access(db, actor, sid)
        return self._session(db, session, member)

    def send(self, actor, sid, body):
        body = _body(body, {"kind", "text", "request_id", "reply_to", "to"})
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
        message = {"kind": kind, "text": text, "reply_to": reply}
        if to is not None:
            message["to"] = to
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
            if kind == "instruction" and to and to.startswith("worker:"):
                target = int(to.split(":", 1)[1])
                if not db.execute("SELECT 1 FROM team_members WHERE session_id=? AND role='worker' "
                                  "AND seat=?", (sid, target)).fetchone():
                    raise ValueError("目标席位不存在")
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
