"""Credential-backed users, agent instances and execution IDs."""
from __future__ import annotations
import hashlib
import json
import math
import re
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class AuthError(Exception):
    def __init__(self, message, status=401, retry_after=None, code=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.code = code


EMAIL_IN_USE = (
    "该邮箱已是另一个账号的身份，本次不会合并两个账号，当前身份和历史保持不变。"
    "未验证的邮箱不能接管他人账号。请改用那个账号已保存的 Bearer，或换一个邮箱。"
    " This email is already an account; nothing was merged."
)
EMAIL_ALREADY_BOUND = (
    "当前账号已绑定其他邮箱，不会改绑，以免拆开已有历史。"
    "清空邮箱会把账号键恢复为原来的 id，历史仍留在该 id 上，之后才能绑定新邮箱。"
    " This account is already bound to a different email."
)


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
            CREATE TABLE IF NOT EXISTS identity_aliases (
              alias TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS ops (
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, agent_id TEXT,
              action TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS ops_user ON ops(user_id, id);
            ''')
            columns={r[1] for r in db.execute('PRAGMA table_info(agents)')}
            if 'member_kind' not in columns: db.execute("ALTER TABLE agents ADD COLUMN member_kind TEXT NOT NULL DEFAULT 'agent'")
            db.execute("CREATE TABLE IF NOT EXISTS contacts (agent_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, email TEXT NOT NULL, purpose TEXT NOT NULL, consent_at REAL NOT NULL)")
            user_columns={r[1] for r in db.execute('PRAGMA table_info(users)')}
            if 'account_key' not in user_columns: db.execute("ALTER TABLE users ADD COLUMN account_key TEXT")
            if 'email' not in user_columns: db.execute("ALTER TABLE users ADD COLUMN email TEXT")
            db.execute("UPDATE users SET account_key=id WHERE account_key IS NULL OR account_key=''")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_account_key ON users(account_key)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email ON users(email) WHERE email IS NOT NULL")
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

    WINDOW = 900
    AUTH_LIMIT = 20
    CONNECT_LIMIT = 60

    def throttle(self, key, limit=None, scope="auth"):
        """Count one attempt per (scope, key); reject without counting past limit.

        `scope` keeps the connect bucket apart from sign-up/sign-in, whose lower
        limit is the password-guessing ceiling. The window is fixed from the first
        attempt; retry_after is the whole seconds left before that window resets.
        """
        limit = self.AUTH_LIMIT if limit is None else limit
        stored = scope + ":" + str(key)
        now = time.time()
        retry_after = None
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE started < ?", (now - self.WINDOW,))
            row = db.execute("SELECT count, started FROM attempts WHERE key=?",
                             (stored,)).fetchone()
            if row is not None and row["count"] >= limit:
                # A rejected attempt must not extend or inflate the window.
                retry_after = max(1, int(math.ceil(row["started"] + self.WINDOW - now)))
            elif row is None:
                db.execute("INSERT INTO attempts VALUES (?, 1, ?)", (stored, now))
            else:
                db.execute("UPDATE attempts SET count=count+1 WHERE key=?", (stored,))
        if retry_after is not None:
            raise AuthError(f"尝试过于频繁，请 {retry_after} 秒后重试", 429,
                            retry_after=retry_after)

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
                db.execute(
                    "INSERT INTO users (id,username,display_name,salt,password,created,account_key) VALUES (?,?,?,?,?,?,?)",
                    (user_id, username, display_name, salt, hashed, time.time(), user_id))
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

    def connect(self, alias, agent_type="访客", member_kind="agent", email=None, contact_consent=False):
        """Mint a durable guest identity. The returned token is the stable key across restarts.

        With no email, account_key is the new user id. With a consented email, that email
        becomes account_key and the user id is kept as an alias. History stays on user_id.
        """
        contact_email = validate_contact(email, contact_consent)
        if contact_email:
            contact_email = contact_email.lower()
        if member_kind not in ("human","agent"): raise ValueError("member_kind 必须为 human 或 agent")
        alias = text(alias, "别名", 60)
        agent_type = text(agent_type, "工具类型", 40)
        user_id = "us_" + secrets.token_hex(12)
        agent_id = "ag_" + secrets.token_hex(12)
        token = "da_" + secrets.token_urlsafe(32)
        now = time.time()
        try:
            with self.db() as db:
                if contact_email and self._email_taken(db, contact_email, None):
                    raise AuthError(EMAIL_IN_USE, 409, code="email_in_use")
                db.execute(
                    "INSERT INTO users (id,username,display_name,salt,password,created,account_key) VALUES (?,?,?,?,?,?,?)",
                    (user_id, "__guest_"+secrets.token_hex(16), alias, secrets.token_hex(16), "disabled", now, user_id))
                db.execute(
                    "INSERT INTO agents (id,user_id,name,type,token_hash,revoked,created,member_kind) VALUES (?,?,?,?,?,0,?,?)",
                    (agent_id, user_id, alias, agent_type, digest(token), now, member_kind))
                self._log(db, user_id, agent_id, "connect", {"alias": alias, "agent_type": agent_type, "member_kind": member_kind})
                if contact_email:
                    self._claim_email(db, user_id, agent_id, contact_email, user_id)
        except sqlite3.IntegrityError:
            raise AuthError(EMAIL_IN_USE, 409, code="email_in_use")
        actor = self.agent(token)
        status = self.account_status(actor)
        return dict(actor, token=token, account_key=status["account_key"], email_bound=status["email_bound"])

    def _email_taken(self, db, email, except_user_id):
        row = db.execute(
            "SELECT id FROM users WHERE (email=? OR account_key=?) AND id!=?",
            (email, email, except_user_id or "")).fetchone()
        return row is not None

    def _log(self, db, user_id, agent_id, action, detail):
        db.execute(
            "INSERT INTO ops (user_id,agent_id,action,detail,created) VALUES (?,?,?,?,?)",
            (user_id, agent_id, action, json.dumps(detail, ensure_ascii=False), time.time()))

    def log_op(self, user_id, agent_id, action, detail):
        with self.db() as db:
            self._log(db, user_id, agent_id, action, detail)

    def _claim_email(self, db, user_id, agent_id, email, prior_key):
        """Point account_key at the email and remember the previous key. Rows stay on user_id."""
        db.execute("UPDATE users SET email=?, account_key=? WHERE id=?", (email, email, user_id))
        db.execute(
            "INSERT INTO identity_aliases (alias,user_id,kind,created) VALUES (?,?,?,?) "
            "ON CONFLICT(alias) DO UPDATE SET user_id=excluded.user_id, kind=excluded.kind, created=excluded.created",
            (prior_key, user_id, "prior_id", time.time()))
        if agent_id:
            db.execute(
                "INSERT INTO contacts VALUES (?,?,?,?,?) "
                "ON CONFLICT(agent_id) DO UPDATE SET email=excluded.email, user_id=excluded.user_id, purpose=excluded.purpose, consent_at=excluded.consent_at",
                (agent_id, user_id, email, "task_followup", time.time()))
        self._log(db, user_id, agent_id, "bind_email", {"from": prior_key, "to": email, "history_kept": True})

    def account_status(self, actor):
        with self.db() as db:
            row = db.execute("SELECT account_key, email FROM users WHERE id=?", (actor["user_id"],)).fetchone()
        key = row["account_key"] if row and row["account_key"] else actor["user_id"]
        return {"user_id": actor["user_id"], "agent_id": actor.get("agent_id"), "account_key": key, "email_bound": bool(row and row["email"])}

    def bind_email(self, actor, email, consent):
        """Bind email as the account key for this user. Same-account history is kept.

        If the email is already another account, raise 409 and change nothing.
        Unverified email never takes over a different account. Repeating the same
        email is a no-op. A different email on an account that already has one is
        also 409; clear it first so the id becomes the account key again.
        """
        if actor.get("kind") not in ("agent", "user"):
            raise AuthError("需要实例凭证", 403)
        normalized = validate_contact(email, consent)
        if not normalized:
            raise ValueError("绑定邮箱需要提供 email")
        normalized = normalized.lower()
        user_id = actor["user_id"]
        agent_id = actor.get("agent_id")
        try:
            with self.db() as db:
                row = db.execute("SELECT id, account_key, email FROM users WHERE id=?", (user_id,)).fetchone()
                if row is None:
                    raise AuthError("账号不存在", 404)
                current = row["email"] or ""
                prior = row["account_key"] or user_id
                if current == normalized:
                    if agent_id:
                        db.execute(
                            "INSERT INTO contacts VALUES (?,?,?,?,?) "
                            "ON CONFLICT(agent_id) DO UPDATE SET email=excluded.email, user_id=excluded.user_id, purpose=excluded.purpose, consent_at=excluded.consent_at",
                            (agent_id, user_id, normalized, "task_followup", time.time()))
                    return self._bind_view(user_id, agent_id, normalized, prior, True)
                if current:
                    raise AuthError(EMAIL_ALREADY_BOUND, 409, code="email_already_bound")
                if self._email_taken(db, normalized, user_id):
                    raise AuthError(EMAIL_IN_USE, 409, code="email_in_use")
                self._claim_email(db, user_id, agent_id, normalized, prior)
                return self._bind_view(user_id, agent_id, normalized, prior, False)
        except sqlite3.IntegrityError:
            raise AuthError(EMAIL_IN_USE, 409, code="email_in_use")

    def _bind_view(self, user_id, agent_id, account_key, prior, reused):
        return {
            "user_id": user_id,
            "agent_id": agent_id,
            "account_key": account_key,
            "prior_account_key": prior,
            "email_bound": True,
            "reused": reused,
            "history_kept": True,
        }

    def unbind_email(self, actor):
        """Release the email as account key and restore the user id. Ops and task rows stay."""
        if actor.get("kind") not in ("agent", "user"):
            raise AuthError("需要实例凭证", 403)
        user_id = actor["user_id"]
        agent_id = actor.get("agent_id")
        with self.db() as db:
            row = db.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
            previous = (row["email"] if row else "") or ""
            if previous:
                db.execute("UPDATE users SET email=NULL, account_key=? WHERE id=?", (user_id, user_id))
                self._log(db, user_id, agent_id, "unbind_email", {"from": previous, "to": user_id, "history_kept": True})
            if agent_id:
                db.execute("DELETE FROM contacts WHERE agent_id=?", (agent_id,))
        return {"user_id": user_id, "agent_id": agent_id, "account_key": user_id, "email_bound": False, "history_kept": True}

    def activity(self, actor, limit=50):
        if actor.get("kind") not in ("agent", "user"):
            raise AuthError("需要实例凭证", 403)
        if isinstance(limit, bool) or not isinstance(limit, int):
            limit = 50
        limit = max(1, min(limit, 100))
        with self.db() as db:
            rows = db.execute(
                "SELECT action, detail, created, agent_id FROM ops WHERE user_id=? ORDER BY id ASC LIMIT ?",
                (actor["user_id"], limit)).fetchall()
            account = db.execute("SELECT account_key FROM users WHERE id=?", (actor["user_id"],)).fetchone()
        ops = []
        for row in rows:
            try:
                detail = json.loads(row["detail"])
            except json.JSONDecodeError:
                detail = {"text": row["detail"]}
            ops.append({"action": row["action"], "detail": detail, "created": row["created"], "agent_id": row["agent_id"]})
        return {
            "user_id": actor["user_id"],
            "account_key": account["account_key"] if account and account["account_key"] else actor["user_id"],
            "ops": ops,
        }

    def rename(self, actor, alias):
        if actor["kind"] != "agent" or not actor.get("guest"):
            raise AuthError("此接口仅用于免注册接入实例",403)
        alias = text(alias, "别名", 60)
        with self.db() as db:
            db.execute("UPDATE agents SET name=? WHERE id=?",(alias,actor["agent_id"]))
            db.execute("UPDATE users SET display_name=? WHERE id=?",(alias,actor["user_id"]))
        return dict(actor, user_name=alias, agent_name=alias)

    def profile(self, actor):
        if actor.get("kind")!="agent":raise AuthError("需要实例凭证",403)
        with self.db() as db:
            row=db.execute("SELECT email,consent_at FROM contacts WHERE agent_id=?",(actor["agent_id"],)).fetchone()
            account=db.execute("SELECT account_key, email FROM users WHERE id=?",(actor["user_id"],)).fetchone()
        return {"alias":actor["agent_name"],"agent_type":actor["agent_type"],"member_kind":actor.get("member_kind","agent"),"email":row["email"] if row else "","contact_consent":bool(row),"email_verified":False,"visibility":"self_and_platform_only","user_id":actor["user_id"],"account_key":(account["account_key"] if account and account["account_key"] else actor["user_id"]),"email_bound":bool(account and account["email"])}

    def update_profile(self, actor, body):
        if actor.get("kind")!="agent" or not actor.get("guest"):raise AuthError("仅可修改自己的免注册实例",403)
        alias=text(body.get("alias",actor["agent_name"]),"别名",60)
        kind=body.get("member_kind",actor.get("member_kind","agent"))
        if kind not in ("human","agent"):raise ValueError("请选择人或智能体")
        tool=text(body.get("agent_type",actor["agent_type"]),"工具类型",40)
        if "email" in body:
            email=validate_contact(body.get("email"),body.get("contact_consent"))
            if email:
                self.bind_email(actor, email, body.get("contact_consent"))
            else:
                self.unbind_email(actor)
        with self.db() as db:
            db.execute("UPDATE agents SET name=?,type=?,member_kind=? WHERE id=? AND user_id=?",(alias,tool,kind,actor["agent_id"],actor["user_id"]))
            db.execute("UPDATE users SET display_name=? WHERE id=?",(alias,actor["user_id"]))
        return dict(actor,agent_name=alias,user_name=alias,agent_type=tool,member_kind=kind)

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


def validate_contact(email, consent):
    if email is None or email == "": return ""
    if not isinstance(email,str) or len(email)>254:raise ValueError("邮箱格式不正确")
    email=email.strip()
    if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+",email):raise ValueError("邮箱格式不正确")
    if consent is not True:raise ValueError("请同意平台仅通过邮箱进行任务相关沟通，或不填邮箱")
    return email
