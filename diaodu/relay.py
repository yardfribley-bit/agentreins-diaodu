"""Private, durable agent relay; it never runs models or system commands."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import io
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


def _safe_name(value):
    """Reject traversal, separators and control characters; the name is display-only."""
    if not isinstance(value, str):
        raise ValueError("文件名不合法")
    name = value.strip()
    if not name or len(name) > 240 or name in (".", ".."):
        raise ValueError("文件名不合法")
    if "/" in name or "\\" in name or any(ord(char) < 32 for char in name):
        raise ValueError("文件名不能包含路径分隔符或控制字符")
    return name


def _content_type(name, head):
    """Both extension and magic bytes must agree; anything else downloads as octet-stream."""
    extension = os.path.splitext(name)[1].lower()
    if extension == ".png" and head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if extension in (".jpg", ".jpeg") and head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if extension == ".gif" and head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if extension == ".webp" and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if extension in (".mp4", ".m4v") and head[4:8] == b"ftyp":
        return "video/mp4"
    if extension == ".webm" and head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if extension in (".txt", ".log", ".md", ".csv", ".json", ".yaml", ".yml") and b"\x00" not in head:
        return "text/plain"
    return "application/octet-stream"


class Relay:
    MAX_EVENTS = 5000
    MAX_OPEN_SESSIONS = 20
    MAX_WAITERS = 32
    PAGE_SIZE = 100
    INVITE_SECONDS = 30 * 60
    ONLINE_SECONDS = 60
    TOUCH_SECONDS = 60
    MAX_FILE_BYTES = 100 * 1024 * 1024
    MAX_ATTACHMENTS = 20
    CONTENT_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp",
                     "video/mp4", "video/webm", "text/plain", "application/octet-stream")

    SIGNED_SECONDS = 15 * 60

    def __init__(self, root: Path, public_base_path: str = "", public_origin: str = ""):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.public_base_path = (public_base_path or "").rstrip("/")
        self.public_origin = (public_origin or "").rstrip("/")
        self.files_dir = root / "relay-files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.key_path = root / "relay-files.key"
        self._signing_key = self._load_signing_key()
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
                  topic_code_hash TEXT, public_read INTEGER NOT NULL DEFAULT 0,
                  public_token_hash TEXT);
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
                CREATE TABLE IF NOT EXISTS revoked (
                  session_id TEXT NOT NULL, actor_key TEXT NOT NULL, role TEXT NOT NULL,
                  revoked_at REAL NOT NULL, PRIMARY KEY(session_id,actor_key));
                CREATE TABLE IF NOT EXISTS files (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, name TEXT NOT NULL,
                  size INTEGER NOT NULL, sha256 TEXT NOT NULL, type TEXT NOT NULL,
                  uploader TEXT NOT NULL, actor_key TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS files_session ON files(session_id);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "topic_code_hash" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN topic_code_hash TEXT")
            if "public_read" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN public_read INTEGER NOT NULL DEFAULT 0")
            if "public_token_hash" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN public_token_hash TEXT")

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

    def _touch(self, sid, key):
        """Keep a listening client online without waking every long poll."""
        now = time.time()
        with self._write(notify=False) as db:
            row = db.execute("SELECT last_seen FROM members WHERE session_id=? AND actor_key=?",
                             (sid, key)).fetchone()
            if row is not None and now - row["last_seen"] >= self.TOUCH_SECONDS:
                db.execute("UPDATE members SET last_seen=? WHERE session_id=? AND actor_key=?",
                           (now, sid, key))

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
                "role": member["role"], "peers": peers, "last_seq": session["last_seq"],
                "public_read": bool(session["public_read"])}

    def _view(self, db, actor, sid):
        session, member = self._access(db, actor, sid)
        return self._session(db, session, member)

    def _load_signing_key(self):
        """Persist the download-signing key under data/library so old links survive restarts."""
        if self.key_path.is_file():
            existing = self.key_path.read_bytes()
            if len(existing) >= 32:
                return existing
        key = secrets.token_bytes(32)
        temporary = self.key_path.with_name(self.key_path.name + ".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, key)
        finally:
            os.close(descriptor)
        os.replace(temporary, self.key_path)
        os.chmod(self.key_path, 0o600)
        return key

    def _base_url(self):
        return (self.public_origin + self.public_base_path) if self.public_origin else self.public_base_path

    def signature(self, session_id, file_id, expires_at=None):
        expires = int(time.time() + self.SIGNED_SECONDS) if expires_at is None else int(expires_at)
        message = f"{session_id}:{file_id}:{expires}".encode()
        return expires, hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()

    def verify_signature(self, session_id, file_id, expires, signature):
        if isinstance(expires, bool) or not isinstance(expires, int):
            return False
        message = f"{session_id}:{file_id}:{expires}".encode()
        expected = hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")

    def signed_url(self, session_id, file_id, expires_at=None):
        expires, signature = self.signature(session_id, file_id, expires_at)
        return (f"{self._base_url()}/v1/relay/sessions/{session_id}/files/{file_id}"
                f"?exp={expires}&sig={signature}")

    def _file_json(self, db, session_id, file_id):
        row = db.execute("SELECT * FROM files WHERE id=? AND session_id=?",
                         (file_id, session_id)).fetchone()
        if row is None:
            return None
        url = self.signed_url(session_id, row["id"])
        return {"id": row["id"], "name": row["name"], "size": row["size"],
                "sha256": row["sha256"], "type": row["type"], "uploader": row["uploader"],
                "created_at": _iso(row["created"]), "url": url, "download_url": url}

    # -- public read-only view ------------------------------------------

    def _public_base(self):
        base = self.public_origin + self.public_base_path
        return base.rstrip("/")

    def _public_row(self, db, hashed):
        row = db.execute("SELECT * FROM sessions WHERE public_token_hash=? AND public_read=1",
                         (hashed,)).fetchone()
        if row is None:
            raise AuthError("公开视图不可用或已关闭", 404)
        return row

    def _public_view(self, db, session):
        members = {row["role"]: row["name"] for row in db.execute(
            "SELECT role,name FROM members WHERE session_id=?", (session["id"],))}
        return {"title": session["title"], "goal": session["goal"], "status": session["status"],
                "created_at": _iso(session["created"]), "updated_at": _iso(session["updated"]),
                "last_seq": session["last_seq"],
                "mentor": {"role": "mentor", "name": members["mentor"]} if "mentor" in members else None,
                "worker": {"role": "worker", "name": members["worker"]} if "worker" in members else None}

    @staticmethod
    def _public_payload(kind, payload, db=None, session_id=None):
        """Whitelist public fields; environment and credentials never leave the server."""
        if kind == "message":
            message = payload.get("message") or {}
            attachments = []
            for item in message.get("attachments") or []:
                if isinstance(item, dict):
                    attachments.append({"name": item.get("name"), "size": item.get("size"),
                                        "type": item.get("type")})
                elif isinstance(item, str) and db is not None:
                    row = db.execute("SELECT name,size,type FROM files WHERE id=? AND session_id=?",
                                     (item, session_id)).fetchone()
                    if row is not None:
                        attachments.append({"name": row["name"], "size": row["size"],
                                            "type": row["type"]})
            public = {"kind": message.get("kind"), "text": message.get("text"),
                      "reply_to": message.get("reply_to")}
            if attachments:
                public["attachments"] = attachments
            return {"message": public}
        if kind == "joined":
            return {"role": payload.get("role")}
        if kind == "state":
            return {"status": payload.get("status")}
        if kind == "revoked":
            return {"role": payload.get("role")}
        return {}

    def _public_event(self, db, row):
        if row["kind"] == "environment":
            return None
        event = {"seq": row["seq"], "kind": row["kind"], "created_at": _iso(row["created"]),
                 "sender": {"role": row["sender_role"], "name": row["sender_name"]}}
        event.update(self._public_payload(row["kind"], json.loads(row["payload"]),
                                         db, row["session_id"]))
        return event

    def _public_page(self, token, after):
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with self._read() as db:
            session = self._public_row(db, hashed)
            if after > session["last_seq"]:
                raise ValueError("消息游标超过当前会话进度")
            rows = db.execute("SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
                              (session["id"], after, self.PAGE_SIZE + 1)).fetchall()
            scanned = rows[:self.PAGE_SIZE]
            events = [event for event in (self._public_event(db, row) for row in scanned)
                      if event is not None]
            return {"session": self._public_view(db, session), "events": events,
                    "next_cursor": scanned[-1]["seq"] if scanned else after,
                    "has_more": len(rows) > self.PAGE_SIZE}

    def public_events(self, token, after=0, wait=0):
        if not isinstance(token, str) or not token or len(token) > 160:
            raise AuthError("公开视图不可用或已关闭", 404)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after须为非负整数")
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or not 0 <= wait <= 20:
            raise ValueError("wait须为0至20秒")
        waiter = ("public", hashlib.sha256(token.encode()).hexdigest())
        registered = False
        deadline = time.monotonic() + wait
        try:
            with self._condition:
                while True:
                    result = self._public_page(token, after)
                    if result["events"] or not wait or result["session"]["status"] == "closed" or time.monotonic() >= deadline:
                        # Re-check the token after waiting: disabling or rotating must close it.
                        with self._read() as db:
                            session = self._public_row(db, waiter[1])
                            result["session"] = self._public_view(db, session)
                        return result
                    if result["next_cursor"] > after:
                        after = result["next_cursor"]
                        continue
                    if not registered:
                        if waiter in self._waiters or len(self._waiters) >= self.MAX_WAITERS:
                            raise AuthError("实时等待连接已达上限，请稍后重试", 429)
                        self._waiters.add(waiter)
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
                raise AuthError("只有管理者可以开关公开围观", 403)
            now = time.time()
            url = None
            if enabled:
                token = secrets.token_urlsafe(24)
                db.execute("UPDATE sessions SET public_read=1, public_token_hash=?, updated=? WHERE id=?",
                           (hashlib.sha256(token.encode()).hexdigest(), now, sid))
                url = self._public_base() + "/relay/watch/" + token
            else:
                db.execute("UPDATE sessions SET public_read=0, public_token_hash=NULL, updated=? WHERE id=?",
                           (now, sid))
            view = self._session(db, db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone(), member)
            return {"session": view, "public_url": url}

    def _event(self, db, row):
        result = {"seq": row["seq"], "kind": row["kind"], "created_at": _iso(row["created"]),
                  "sender": {"role": row["sender_role"], "name": row["sender_name"]}}
        payload = json.loads(row["payload"])
        message = payload.get("message")
        if isinstance(message, dict):
            # Old events predate attachments: readers always get a list, never a KeyError.
            ids = message.get("attachments") or []
            message["attachments"] = [item for item in
                                      (self._file_json(db, row["session_id"], file_id) for file_id in ids)
                                      if item is not None]
        result.update(payload)
        return result

    def _append(self, db, sid, member, kind, payload, *, request_id=None, content_hash=None,
                optional=False, critical=False):
        last = db.execute("SELECT last_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]
        # Ordinary content stops one slot before the cap; control events (state, revoke)
        # may still land so a full session can always be paused or closed. Optional
        # events (environment refresh) are dropped silently instead of failing.
        if not critical and last >= self.MAX_EVENTS - 1:
            if optional:
                return None
            raise AuthError("会话事件数量已达上限，请结束后新建会话", 409)
        now = time.time()
        seq = last + 1
        db.execute("UPDATE sessions SET last_seq=?,updated=? WHERE id=?", (seq, now, sid))
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (sid, seq, kind, now, member["role"], member["name"],
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    member["actor_key"], request_id, content_hash))
        return self._event(db, db.execute("SELECT * FROM events WHERE session_id=? AND seq=?", (sid, seq)).fetchone())

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
            # Topic codes are shared and stay valid until rotated or the session closes,
            # so a revoked identity must not be able to reuse one to come back. A fresh
            # one-time invite issued by the owner (revoke deletes the old ones) is the
            # only way to readmit the same identity, and that is an explicit owner act.
            if invite is None and db.execute(
                    "SELECT 1 FROM revoked WHERE session_id=? AND actor_key=?", (sid, key)).fetchone():
                raise AuthError("该身份已被管理者撤销，不能用 Topic 码重新加入", 403)
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
            db.execute("DELETE FROM revoked WHERE session_id=? AND actor_key=?", (sid, key))
            member = db.execute("SELECT * FROM members WHERE session_id=? AND role=?", (sid, role)).fetchone()
            self._append(db, sid, member, "joined", {"role": role})
            return {"session": self._view(db, actor, sid)}

    def get(self, actor, sid):
        with self._read() as db:
            return {"session": self._view(db, actor, sid)}

    def topic_info(self, code):
        """Public view of a Topic code: title/goal/status plus seat occupancy, no identities."""
        if not isinstance(code, str):
            raise AuthError("配对码无效或已失效", 404)
        code = code.strip()
        if not code or len(code) > 160:
            raise AuthError("配对码无效或已失效", 404)
        hashed = hashlib.sha256(code.encode()).hexdigest()
        with self._read() as db:
            session = db.execute("SELECT * FROM sessions WHERE topic_code_hash=?", (hashed,)).fetchone()
            if session is None:
                raise AuthError("配对码无效或已失效", 404)
            found = {row["role"]: row for row in db.execute(
                "SELECT role,name FROM members WHERE session_id=?", (session["id"],))}
        seats = {role: ({"occupied": True, "name": found[role]["name"]} if role in found
                        else {"occupied": False, "name": None}) for role in ("mentor", "worker")}
        return {"title": session["title"], "goal": session["goal"], "status": session["status"],
                "created_at": _iso(session["created"]), "seats": seats}

    def _page(self, actor, sid, after):
        with self._read() as db:
            session, member = self._access(db, actor, sid)
            if after > session["last_seq"]:
                raise ValueError("消息游标超过当前会话进度")
            rows = db.execute("SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
                              (sid, after, self.PAGE_SIZE + 1)).fetchall()
            batch = rows[:self.PAGE_SIZE]
            return {"session": self._session(db, session, member), "events": [self._event(db, row) for row in batch],
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
                        # A listening client is alive even when it never sends: refresh
                        # presence (60s throttle) before the final membership recheck.
                        self._touch(sid, key)
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
        body = _body(body, {"kind", "text", "request_id", "reply_to", "attachments"})
        kind = body.get("kind")
        if kind not in ("instruction", "progress", "result", "question", "note"):
            raise ValueError("未知消息类型")
        text = _text(body.get("text"), "消息", 8000)
        attachments = body.get("attachments", [])
        if (not isinstance(attachments, list) or len(attachments) > self.MAX_ATTACHMENTS
                or any(not isinstance(item, str) for item in attachments)):
            raise ValueError(f"附件最多{self.MAX_ATTACHMENTS}个，且须为附件编号")
        attachments = list(dict.fromkeys(attachments))
        message = {"kind": kind, "text": text, "reply_to": body.get("reply_to")}
        # Only add the field when used: hashes of attachment-free messages stay identical
        # to the pre-attachment format, so old request_id retries still deduplicate.
        if attachments:
            message["attachments"] = attachments
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
                return {"event": self._event(db, duplicate), "duplicate": True}
            if attachments:
                placeholders = ",".join("?" for _ in attachments)
                found = db.execute(f"SELECT COUNT(*) FROM files WHERE session_id=? AND id IN ({placeholders})",
                                   (sid, *attachments)).fetchone()[0]
                if found != len(attachments):
                    raise ValueError("附件不属于本会话")
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

    def _files_member(self, db, actor, sid):
        """Membership for attachment routes: 404 for an unknown session, 403 for outsiders."""
        key = _actor(actor)
        if not isinstance(sid, str) or not sid or len(sid) > 160:
            raise AuthError("没有这个会话", 404)
        session = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if session is None:
            raise AuthError("没有这个会话", 404)
        member = db.execute("SELECT * FROM members WHERE session_id=? AND actor_key=?", (sid, key)).fetchone()
        if member is None:
            raise AuthError("不是该会话成员，不能访问附件", 403)
        return session, member

    def require_member(self, actor, sid):
        with self._read() as db:
            self._files_member(db, actor, sid)

    def _register(self, actor, sid, name, size, digest, content_type, temporary):
        if size <= 0:
            raise ValueError("文件不能为空")
        if size > self.MAX_FILE_BYTES:
            raise ValueError("单个文件不能超过 100 MB")
        key = _actor(actor)
        with self._write() as db:
            session, member = self._files_member(db, actor, sid)
            if session["status"] == "closed":
                raise AuthError("会话已结束，不能上传附件", 409)
            file_id = "fl_" + secrets.token_hex(12)
            target = self.files_dir / file_id
            os.replace(temporary, target)
            try:
                db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                           (file_id, sid, name, size, digest, content_type, member["name"], key, time.time()))
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return self._file_json(db, sid, file_id)

    def receive(self, actor, sid, name, reader, length):
        """Stream bytes to disk (hashing) outside the write lock, then register metadata."""
        name = _safe_name(name)
        if length <= 0:
            raise ValueError("文件不能为空")
        if length > self.MAX_FILE_BYTES:
            raise ValueError("单个文件不能超过 100 MB")
        self.require_member(actor, sid)
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
                                  _content_type(name, head), temporary)
        finally:
            temporary.unlink(missing_ok=True)

    def upload(self, actor, sid, name, content):
        """Bytes already in memory (MCP path, <=10MB); hashing and disk write happen unlocked."""
        if not isinstance(content, (bytes, bytearray)):
            raise ValueError("文件内容不合法")
        return self.receive(actor, sid, name, io.BytesIO(bytes(content)), len(content))

    def _locate(self, sid, file_id):
        with self._read() as db:
            row = db.execute("SELECT * FROM files WHERE id=? AND session_id=?", (file_id, sid)).fetchone()
            if row is None:
                raise AuthError("没有这个附件", 404)
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
        """Only for a verified signed link; callers must check the HMAC first."""
        return self._locate(sid, file_id)

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
                    self._append(db, sid, member, "environment", {"role": "worker", "environment": environment},
                                 optional=True)
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
                self._append(db, sid, member, "state", {"status": status}, critical=True)
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
            person = db.execute("SELECT actor_key FROM members WHERE session_id=? AND role=?", (sid, role)).fetchone()
            db.execute("DELETE FROM invites WHERE session_id=? AND role=?", (sid, role))
            if person:
                # After close the history is read-only and the revoked member can no
                # longer read it anyway, so no new event is needed; while the session
                # is open the revocation event must survive a full event log.
                if session["status"] != "closed":
                    self._append(db, sid, member, "revoked", {"role": role}, critical=True)
                db.execute("DELETE FROM members WHERE session_id=? AND role=?", (sid, role))
                db.execute("INSERT INTO revoked VALUES (?,?,?,?) ON CONFLICT(session_id,actor_key) "
                           "DO UPDATE SET role=excluded.role, revoked_at=excluded.revoked_at",
                           (sid, person["actor_key"], role, time.time()))
                db.execute("UPDATE sessions SET updated=? WHERE id=?", (time.time(), sid))
            return {"session": self._view(db, actor, sid)}
