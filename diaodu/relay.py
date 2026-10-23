"""Private, durable agent relay; it never runs models or system commands."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from diaodu.identity import AuthError


def _iso(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _body(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("请求字段不合法，身份与角色由服务端确定")
    return value


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(label + "不能为空或超过长度限制")
    return value.strip()


def _actor(actor):
    if not isinstance(actor, dict):
        raise AuthError("请先使用有效凭证接入")
    agent = actor.get("agent_id")
    user = actor.get("user_id")
    if isinstance(agent, str) and agent:
        return "agent:" + agent
    if actor.get("kind") != "agent" and isinstance(user, str) and user:
        return "user:" + user
    raise AuthError("请先使用有效凭证接入")


def _name(actor):
    for key in ("agent_name", "user_name", "display_name"):
        value = actor.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:160]
    return "参与者"


def _environment(value):
    value = _body(value, {"os_name", "os_version", "architecture", "shells", "python_version"})
    result = {key: _text(value.get(key), key, 160)
              for key in ("os_name", "os_version", "architecture")}
    if "shells" in value:
        shells = value["shells"]
        if not isinstance(shells, list) or len(shells) > 16:
            raise ValueError("shells须为最多16项的字符串数组")
        result["shells"] = [_text(item, "shell", 40) for item in shells]
    if "python_version" in value:
        result["python_version"] = _text(value["python_version"], "python_version", 80)
    return result


class Relay:
    MAX_EVENTS = 5000
    MAX_OPEN_SESSIONS = 20
    MAX_WAITERS = 32
    PAGE_SIZE = 100
    INVITE_SECONDS = 30 * 60
    ONLINE_SECONDS = 60

    def __init__(self, root: Path):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "relay.sqlite3"
        # Create privately before SQLite opens it; the caller's umask is irrelevant.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        self._condition = threading.Condition(threading.RLock())
        self._waiters = set()
        with self._write() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL,
                  status TEXT NOT NULL, owner_key TEXT NOT NULL,
                  created REAL NOT NULL, updated REAL NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
                  topic_code_hash TEXT);
                CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(owner_key,status);
                CREATE TABLE IF NOT EXISTS members (
                  session_id TEXT NOT NULL, role TEXT NOT NULL, actor_key TEXT NOT NULL,
                  name TEXT NOT NULL, last_seen REAL NOT NULL, ack_seq INTEGER NOT NULL DEFAULT 0,
                  environment TEXT, PRIMARY KEY(session_id,role), UNIQUE(session_id,actor_key));
                CREATE INDEX IF NOT EXISTS members_actor ON members(actor_key,session_id);
                CREATE TABLE IF NOT EXISTS invites (
                  code_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
                  expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS invites_session ON invites(session_id,role);
                CREATE TABLE IF NOT EXISTS events (
                  session_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
                  created REAL NOT NULL, sender_role TEXT NOT NULL, sender_name TEXT NOT NULL,
                  payload TEXT NOT NULL, sender_key TEXT, request_id TEXT, content_hash TEXT,
                  PRIMARY KEY(session_id,seq), UNIQUE(session_id,sender_key,request_id));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "topic_code_hash" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN topic_code_hash TEXT")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
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
    def _write(self):
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
                if committed:
                    self._condition.notify_all()

    def _access(self, db, actor, sid):
        key = _actor(actor)
        if not isinstance(sid, str) or not sid or len(sid) > 160:
            raise AuthError("没有这个会话或无权访问", 404)
        session = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        member = db.execute("SELECT * FROM members WHERE session_id=? AND actor_key=?", (sid, key)).fetchone()
        if session is None or member is None:
            raise AuthError("没有这个会话或无权访问", 404)
        return session, member

    def _owner(self, member):
        if member["role"] != "owner":
            raise AuthError("只有管理者可以执行此操作", 403)

    def _session(self, db, session, member):
        found = {row["role"]: row for row in db.execute(
            "SELECT * FROM members WHERE session_id=?", (session["id"],))}
        now = time.time()
        peers = []
        for role in ("owner", "mentor", "worker"):
            person = found.get(role)
            item = {"role": role, "name": person["name"] if person else "",
                    "online": bool(person and now - person["last_seen"] < self.ONLINE_SECONDS),
                    "last_seen": _iso(person["last_seen"]) if person else None,
                    "ack_seq": person["ack_seq"] if person else 0}
            if role == "worker":
                item["environment"] = json.loads(person["environment"]) if person and person["environment"] else None
            peers.append(item)
        return {"id": session["id"], "title": session["title"], "goal": session["goal"],
                "status": session["status"], "created_at": _iso(session["created"]),
                "updated_at": _iso(session["updated"]), "owner": member["role"] == "owner",
                "role": member["role"], "peers": peers, "last_seq": session["last_seq"]}

    def _view(self, db, actor, sid):
        session, member = self._access(db, actor, sid)
        return self._session(db, session, member)

    def _event(self, row):
        result = {"seq": row["seq"], "kind": row["kind"], "created_at": _iso(row["created"]),
                  "sender": {"role": row["sender_role"], "name": row["sender_name"]}}
        result.update(json.loads(row["payload"]))
        return result

    def _append(self, db, sid, member, kind, payload, *, request_id=None, content_hash=None, allow_full=False):
        last = db.execute("SELECT last_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]
        closing = kind == "state" and payload.get("status") == "closed"
        limit = self.MAX_EVENTS if closing else self.MAX_EVENTS - 1
        if last >= limit:
            if allow_full:
                return None
            raise AuthError("会话事件数量已达上限，请结束后新建会话", 409)
        now = time.time()
        seq = last + 1
        db.execute("UPDATE sessions SET last_seq=?,updated=? WHERE id=?", (seq, now, sid))
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (sid, seq, kind, now, member["role"], member["name"],
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    member["actor_key"], request_id, content_hash))
        return self._event(db.execute("SELECT * FROM events WHERE session_id=? AND seq=?", (sid, seq)).fetchone())

    def _issue(self, db, sid, role):
        code = secrets.token_urlsafe(24)
        expires = time.time() + self.INVITE_SECONDS
        db.execute("DELETE FROM invites WHERE session_id=? AND role=?", (sid, role))
        db.execute("INSERT INTO invites VALUES (?,?,?,?)",
                   (hashlib.sha256(code.encode()).hexdigest(), sid, role, expires))
        return {"role": role, "code": code, "expires_at": _iso(expires)}

    def list_sessions(self, actor):
        key = _actor(actor)
        with self._read() as db:
            rows = db.execute("SELECT s.* FROM sessions s JOIN members m ON m.session_id=s.id "
                              "WHERE m.actor_key=? ORDER BY s.updated DESC,s.id", (key,)).fetchall()
            result = []
            for row in rows:
                member = db.execute("SELECT * FROM members WHERE session_id=? AND actor_key=?", (row["id"], key)).fetchone()
                result.append(self._session(db, row, member))
            return {"sessions": result}

    def create(self, actor, body):
        key = _actor(actor)
        body = _body(body, {"title", "goal"})
        title = _text(body.get("title"), "标题", 160)
        goal = _text(body.get("goal"), "目标", 8000)
        now = time.time()
        sid = "rl_" + secrets.token_hex(16)
        topic_code = secrets.token_urlsafe(24)
        with self._write() as db:
            count = db.execute("SELECT COUNT(*) FROM sessions WHERE owner_key=? AND status!='closed'", (key,)).fetchone()[0]
            if count >= self.MAX_OPEN_SESSIONS:
                raise AuthError("每个管理者最多20个未关闭会话", 429)
            db.execute("INSERT INTO sessions (id,title,goal,status,owner_key,created,updated,last_seq,topic_code_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                       (sid, title, goal, "active", key, now, now, 0,
                        hashlib.sha256(topic_code.encode()).hexdigest()))
            db.execute("INSERT INTO members VALUES (?,?,?,?,?,?,?)", (sid, "owner", key, _name(actor), now, 0, None))
            return {"session": self._view(db, actor, sid), "topic_code": topic_code}

    def join(self, actor, body):
        key = _actor(actor)
        body = _body(body, {"code", "role", "environment"})
        code = _text(body.get("code"), "配对码", 160)
        hashed = hashlib.sha256(code.encode()).hexdigest()
        with self._write() as db:
            invite = db.execute("SELECT * FROM invites WHERE code_hash=?", (hashed,)).fetchone()
            topic = None
            if invite is None:
                topic = db.execute("SELECT * FROM sessions WHERE topic_code_hash=?", (hashed,)).fetchone()
                if topic is None:
                    raise AuthError("Topic 配对码无效或已失效", 403)
                role = body.get("role")
                if role not in ("mentor", "worker"):
                    raise ValueError("使用 Topic 配对码时必须选择 mentor 或 worker 角色")
                sid = topic["id"]
            else:
                if invite["expires"] <= time.time():
                    raise AuthError("配对码无效或已失效", 403)
                sid, role = invite["session_id"], invite["role"]
                if body.get("role") is not None and body.get("role") != role:
                    raise ValueError("所选角色与此一次性配对码不一致")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if session is None or session["status"] == "closed":
                raise AuthError("配对码无效或已失效", 403)
            if db.execute("SELECT 1 FROM members WHERE session_id=? AND (actor_key=? OR role=?)", (sid, key, role)).fetchone():
                raise AuthError("该身份已参与会话或角色已配对", 409)
            if role != "worker" and "environment" in body:
                raise AuthError("只有执行端可以上报系统环境", 403)
            environment = _environment(body.get("environment")) if role == "worker" else None
            now = time.time()
            db.execute("INSERT INTO members VALUES (?,?,?,?,?,?,?)",
                       (sid, role, key, _name(actor), now, 0,
                        json.dumps(environment, sort_keys=True, ensure_ascii=False) if environment else None))
            if invite is not None:
                db.execute("DELETE FROM invites WHERE session_id=? AND role=?", (sid, role))
            member = db.execute("SELECT * FROM members WHERE session_id=? AND role=?", (sid, role)).fetchone()
            self._append(db, sid, member, "joined", {"role": role})
            return {"session": self._view(db, actor, sid)}

    def get(self, actor, sid):
        with self._read() as db:
            return {"session": self._view(db, actor, sid)}

    def _page(self, actor, sid, after):
        with self._read() as db:
            session, member = self._access(db, actor, sid)
            if after > session["last_seq"]:
                raise ValueError("消息游标超过当前会话进度")
            rows = db.execute("SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
                              (sid, after, self.PAGE_SIZE + 1)).fetchall()
            batch = rows[:self.PAGE_SIZE]
            return {"session": self._session(db, session, member), "events": [self._event(row) for row in batch],
                    "next_cursor": batch[-1]["seq"] if batch else after, "has_more": len(rows) > self.PAGE_SIZE}

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
                        # Recheck in a fresh transaction immediately before returning data.
                        with self._read() as db:
                            result["session"] = self._view(db, actor, sid)
                        return result
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

    def send(self, actor, sid, body):
        body = _body(body, {"kind", "text", "request_id", "reply_to"})
        kind = body.get("kind")
        if kind not in ("instruction", "progress", "result", "question", "note"):
            raise ValueError("未知消息类型")
        _text(body.get("text"), "消息", 8000)
        message = {"kind": kind, "text": body["text"], "reply_to": body.get("reply_to")}
        request_id = _text(body.get("request_id"), "request_id", 160)
        reply = message["reply_to"]
        if reply is not None and (isinstance(reply, bool) or not isinstance(reply, int) or reply < 1):
            raise ValueError("reply_to须为本会话消息序号")
        hashed = hashlib.sha256(json.dumps(message, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            duplicate = db.execute("SELECT * FROM events WHERE session_id=? AND sender_key=? AND request_id=?",
                                   (sid, member["actor_key"], request_id)).fetchone()
            if duplicate:
                if duplicate["content_hash"] != hashed:
                    raise AuthError("相同request_id不能对应不同消息", 409)
                return {"event": self._event(duplicate), "duplicate": True}
            if session["status"] == "closed":
                raise AuthError("会话已结束，不能发送新消息", 409)
            if kind == "instruction" and member["role"] not in ("mentor", "owner"):
                raise AuthError("只有指导端或管理者可以发送指令", 403)
            if kind in ("progress", "result") and member["role"] != "worker":
                raise AuthError("只有执行端可以回报进度或结果", 403)
            if kind == "instruction" and session["status"] == "paused":
                raise AuthError("会话已暂停，不能发送新指令", 409)
            if reply is not None and (reply > session["last_seq"] or not db.execute(
                    "SELECT 1 FROM events WHERE session_id=? AND seq=?", (sid, reply)).fetchone()):
                raise ValueError("reply_to不属于本会话历史")
            db.execute("UPDATE members SET last_seen=? WHERE session_id=? AND actor_key=?", (time.time(), sid, member["actor_key"]))
            event = self._append(db, sid, member, "message", {"message": message}, request_id=request_id, content_hash=hashed)
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
            db.execute("UPDATE members SET ack_seq=?,last_seen=? WHERE session_id=? AND actor_key=?", (seq, time.time(), sid, member["actor_key"]))
            return {"acknowledged": seq}

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
                    self._append(db, sid, member, "environment", {"role": "worker", "environment": environment})
                    db.execute("UPDATE members SET environment=? WHERE session_id=? AND role='worker'",
                               (json.dumps(environment, sort_keys=True, ensure_ascii=False), sid))
            db.execute("UPDATE members SET last_seen=? WHERE session_id=? AND actor_key=?", (time.time(), sid, member["actor_key"]))
            return {"session": self._view(db, actor, sid)}

    def state(self, actor, sid, body):
        body = _body(body, {"status"})
        status = body.get("status")
        if status not in ("active", "paused", "closed"):
            raise ValueError("未知会话状态")
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            self._owner(member)
            if session["status"] == "closed" and status != "closed":
                raise AuthError("已结束的会话不能重新开启", 409)
            if status != session["status"]:
                self._append(db, sid, member, "state", {"status": status})
                db.execute("UPDATE sessions SET status=? WHERE id=?", (status, sid))
                if status == "closed":
                    db.execute("DELETE FROM invites WHERE session_id=?", (sid,))
            return {"session": self._view(db, actor, sid)}

    def invite(self, actor, sid, body):
        body = _body(body, {"role"})
        role = body.get("role")
        if role not in ("mentor", "worker"):
            raise ValueError("角色须为mentor或worker")
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            self._owner(member)
            if session["status"] == "closed":
                raise AuthError("会话已结束", 409)
            if db.execute("SELECT 1 FROM members WHERE session_id=? AND role=?", (sid, role)).fetchone():
                raise AuthError("角色已配对，请先撤销成员", 409)
            return self._issue(db, sid, role)

    def rotate_topic_code(self, actor, sid, body):
        _body(body, set())
        code = secrets.token_urlsafe(24)
        hashed = hashlib.sha256(code.encode()).hexdigest()
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            self._owner(member)
            if session["status"] == "closed":
                raise AuthError("Topic 已结束，不能重置配对码", 409)
            db.execute("UPDATE sessions SET topic_code_hash=?,updated=? WHERE id=?", (hashed,time.time(),sid))
            return {"code": code}

    def revoke(self, actor, sid, body):
        body = _body(body, {"role"})
        role = body.get("role")
        if role not in ("mentor", "worker"):
            raise ValueError("角色须为mentor或worker")
        with self._write() as db:
            session, member = self._access(db, actor, sid)
            self._owner(member)
            person = db.execute("SELECT 1 FROM members WHERE session_id=? AND role=?", (sid, role)).fetchone()
            db.execute("DELETE FROM invites WHERE session_id=? AND role=?", (sid, role))
            if person:
                self._append(db, sid, member, "revoked", {"role": role}, allow_full=True)
                db.execute("DELETE FROM members WHERE session_id=? AND role=?", (sid, role))
                db.execute("UPDATE sessions SET updated=? WHERE id=?", (time.time(), sid))
            return {"session": self._view(db, actor, sid)}
