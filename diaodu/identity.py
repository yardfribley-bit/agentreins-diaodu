"""Credential-backed users, agent instances and execution IDs."""
from __future__ import annotations
import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def text(value, field, limit=100):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} 必须是 1 到 {limit} 字符")
    return value.strip()


class Identity:
    def __init__(self, path: Path, invite_code=""):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.invite_code = path, invite_code
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
              salt TEXT NOT NULL, password TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
              hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS agents (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
              token_hash TEXT UNIQUE NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, agent_id TEXT NOT NULL,
              created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (
              key TEXT PRIMARY KEY, count INTEGER NOT NULL, started REAL NOT NULL);
            ''')
            columns={r[1] for r in db.execute('PRAGMA table_info(agents)')}
            if 'member_kind' not in columns: db.execute("ALTER TABLE agents ADD COLUMN member_kind TEXT NOT NULL DEFAULT 'agent'")
        path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def throttle(self, key):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE started < ?", (time.time()-900,))
            db.execute("INSERT INTO attempts VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE SET count=count+1", (key, time.time()))
            count = db.execute("SELECT count FROM attempts WHERE key=?", (key,)).fetchone()[0]
        if count > 20:
            raise AuthError("尝试过于频繁，请 15 分钟后重试", 429)

    def register(self, username, display_name, password, invite):
        if not self.invite_code or not isinstance(invite, str) or not hmac.compare_digest(invite.encode(), self.invite_code.encode()):
            raise AuthError("需要有效的邀请码", 403)
        username = text(username, "用户名", 64).lower()
        if not all(c.isascii() and (c.isalnum() or c in "_-.") for c in username):
            raise ValueError("用户名只能包含英文字母、数字、下划线、点和横线")
        display_name = text(display_name, "显示名称", 60)
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError("密码长度必须是 12 到 256 字符")
        user_id, salt = "us_"+secrets.token_hex(12), secrets.token_hex(16)
        hashed = password_hash(password, salt)
        try:
            with self.db() as db:
                db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (user_id, username, display_name, salt, hashed, time.time()))
        except sqlite3.IntegrityError:
            raise AuthError("用户名已被使用", 409)
        return {"user_id": user_id, "username": username, "display_name": display_name}

    def login(self, username, password):
        username = text(username, "用户名", 64).lower()
        if not isinstance(password, str) or len(password) > 256:
            raise ValueError("密码不合法")
        with self.db() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        salt = row["salt"] if row else "00"*16
        hashed = password_hash(password, salt)
        if row is None or not hmac.compare_digest(hashed, row["password"]):
            raise AuthError("用户名或密码错误")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (digest(token), row["id"], csrf, time.time()+86400))
        return token, csrf

    def session(self, token):
        with self.db() as db:
            row = db.execute("SELECT u.id user_id,u.username,u.display_name,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.expires>?", (digest(token), time.time())).fetchone()
        if not row:
            raise AuthError("请登录")
        return dict(row, kind="user")

    def logout(self, token):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (digest(token),))

    def create_agent(self, user_id, name, agent_type):
        name, agent_type = text(name, "实例名称", 60), text(agent_type, "工具类型", 40)
        token, agent_id = "da_"+secrets.token_urlsafe(32), "ag_"+secrets.token_hex(12)
        with self.db() as db:
            db.execute("INSERT INTO agents (id,user_id,name,type,token_hash,revoked,created) VALUES (?,?,?,?,?,0,?)", (agent_id,user_id,name,agent_type,digest(token),time.time()))
        return {"agent_id": agent_id, "user_id":user_id, "agent_name":name,"agent_type":agent_type,"token":token}

    def agents(self, user_id):
        with self.db() as db:
            rows = db.execute("SELECT id agent_id,user_id,name agent_name,type agent_type,revoked,created FROM agents WHERE user_id=? ORDER BY created DESC",(user_id,)).fetchall()
        return [dict(row) for row in rows]

    def revoke(self, user_id, agent_id):
        with self.db() as db:
            result = db.execute("UPDATE agents SET revoked=1 WHERE id=? AND user_id=?", (agent_id,user_id))
            if not result.rowcount:
                raise AuthError("无法管理其他人员的实例", 403)
        return {"revoked": True, "agent_id":agent_id}

    def agent(self, token):
        with self.db() as db:
            row = db.execute("SELECT a.id agent_id,a.name agent_name,a.type agent_type,a.member_kind,u.id user_id,u.display_name user_name,u.username FROM agents a JOIN users u ON u.id=a.user_id WHERE a.token_hash=? AND a.revoked=0", (digest(token),)).fetchone()
        if not row:
            raise AuthError("智能体凭证无效或已撤销")
        result=dict(row, kind="agent")
        result["guest"] = result.pop("username").startswith("__guest_")
        return result

    def connect(self, alias, agent_type="访客", member_kind="agent"):
        if member_kind not in ("human","agent"): raise ValueError("member_kind 必须为 human 或 agent")
        alias = text(alias, "别名", 60)
        agent_type = text(agent_type, "工具类型", 40)
        user_id = "us_" + secrets.token_hex(12)
        agent_id = "ag_" + secrets.token_hex(12)
        token = "da_" + secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (user_id,"__guest_"+secrets.token_hex(16),alias,secrets.token_hex(16),"disabled",time.time()))
            db.execute("INSERT INTO agents (id,user_id,name,type,token_hash,revoked,created) VALUES (?,?,?,?,?,0,?)", (agent_id,user_id,alias,agent_type,digest(token),time.time()))
        with self.db() as db: db.execute("UPDATE agents SET member_kind=? WHERE id=?",(member_kind,agent_id))
        return dict(self.agent(token), token=token)

    def rename(self, actor, alias):
        if actor["kind"] != "agent" or not actor.get("guest"):
            raise AuthError("此接口仅用于免注册接入实例",403)
        alias = text(alias, "别名", 60)
        with self.db() as db:
            db.execute("UPDATE agents SET name=? WHERE id=?",(alias,actor["agent_id"]))
            db.execute("UPDATE users SET display_name=? WHERE id=?",(alias,actor["user_id"]))
        return dict(actor, user_name=alias, agent_name=alias)

    def start_run(self, actor, task_id):
        run_id = "run_"+secrets.token_hex(16)
        with self.db() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?)",(run_id,task_id,actor["agent_id"],time.time()))
        return dict(actor, run_id=run_id, task_id=task_id)

    def run(self, actor, task_id, run_id):
        if not isinstance(run_id,str):
            raise ValueError("必须提供 run_id，先创建本次执行")
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=? AND task_id=? AND agent_id=?",(run_id,task_id,actor["agent_id"])).fetchone()
        if not row:
            raise AuthError("本次执行不属于该智能体或该任务",403)
        return dict(actor,run_id=run_id)
