"""Luckin promo. The live menu is whatever a coupon can fully cover.

Hub does not store Luckin account tokens or coupon codes, and it does not
speak the Luckin API. A private media service holds the Luckin account. Hub
asks that service for the drinks it can order with needPay false, then places
one of those drinks. The reply is a pickup code and/or a pickup QR. needPay
or any error fails the claim and frees the day's cup. There is no chat
confirmation and no agent-type whitelist on this path.

Contacts are stored sealed. Public JSON only gets a mask. Coupon codes and
payment QR links that arrive on a webhook reply are dropped and never stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_LIMIT = 2
PERSON_LIMIT = 1
ACTIVE = ("pending", "ordering", "ready")
STATUSES = ("pending", "ordering", "ready", "failed", "rejected", "cancelled")
LABELS = {
    "pending": "正在下单",
    "ordering": "正在下单",
    "ready": "可取餐",
    "failed": "下单未成功，今日名额已释放",
    "rejected": "未通过，今日名额已释放",
    "cancelled": "已取消，今日名额已释放",
}
PAY_COUPON = "coupon"
PAY_POLICY = "coupon_only"
NO_COUPONS = "现在没有可用的券，暂时不能请客。"
MENU_TTL = 60.0
MENU_TIMEOUT = 8
ORDER_TIMEOUT = 20
SIGNATURE_SKEW = 300

# Known product ids are a mapping helper only. A drink is offered when the
# live menu lists it, never because it appears here. americano matches the
# product id already used on the media account.
DRINKS = {
    "coconut_latte": {
        "name": "生椰拿铁",
        "product_id": "1262",
        "sku": "SP2077-01134",
        "spec": "大杯/冰/意式拼配/默认浓度/不另外加糖/无奶油",
    },
    "americano": {
        "name": "标准美式",
        "product_id": "2500",
        "sku": "SP2277-03135",
        "spec": "大杯/冰",
    },
    "latte": {"name": "普通拿铁", "product_id": "", "sku": "", "spec": ""},
    "butter_latte": {"name": "小黄油拿铁", "product_id": "", "sku": "", "spec": ""},
    "butter_americano": {"name": "小黄油美式", "product_id": "", "sku": "", "spec": ""},
    "ethiopia_americano": {"name": "埃塞金烘美式", "product_id": "", "sku": "", "spec": ""},
    "lemon_tea": {"name": "手打鲜切柠檬茶", "product_id": "", "sku": "", "spec": ""},
}

CLAIM_FIELDS = {
    "phone", "email", "city", "shopName", "locationNote", "lat", "lng",
    "drink", "drink_id", "drinkId", "consent",
}
# Fields the media worker needs to call Luckin previewOrder / createOrder.
# No phone, email, coupon code, or Luckin token. resultUrl is the guest's
# short link; the worker must keep that endpoint private.
ORDER_FIELDS = {
    "action", "payWith", "payPolicy", "needPay", "claimId", "resultUrl", "contactMasked",
    "city", "shopName", "locationNote", "latitude", "longitude",
    "drink", "drinkId", "productId", "skuCode", "amount", "spec",
    "productList", "createdAt",
}


class LuckinError(Exception):
    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def fulfiller_ids():
    raw = os.environ.get("LUCKIN_FULFILLER_AGENT_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def order_webhook_url():
    """Media order endpoint. The new name wins; the pending URL still works."""
    return (
        os.environ.get("LUCKIN_ORDER_WEBHOOK_URL")
        or os.environ.get("LUCKIN_PENDING_WEBHOOK_URL")
        or ""
    ).strip()


def webhook_secret():
    return os.environ.get("LUCKIN_WEBHOOK_SECRET", "").strip()


def menu_webhook_url():
    """Menu endpoint. An explicit URL wins; otherwise it is the order URL's sibling /menu."""
    explicit = os.environ.get("LUCKIN_MENU_WEBHOOK_URL", "").strip()
    if explicit:
        return explicit
    return sibling_menu_url(order_webhook_url())


def sibling_menu_url(order_url):
    parsed = urlparse(order_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    path = parsed.path or ""
    if path.endswith("/"):
        path = path[:-1]
    parent, slash, _last = path.rpartition("/")
    menu_path = (parent + "/menu") if slash else "/menu"
    return urlunparse((parsed.scheme, parsed.netloc, menu_path, "", "", ""))


def sign_webhook(secret, timestamp, body):
    """HMAC-SHA256 over ``timestamp + '.' + raw body``. ``body`` is the exact POST bytes."""
    message = str(int(timestamp)).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def webhook_headers(body, secret, timestamp):
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "agentreins-hub-luckin",
    }
    if secret:
        headers["X-Luckin-Timestamp"] = str(int(timestamp))
        headers["X-Luckin-Signature"] = sign_webhook(secret, timestamp, body)
    return headers


def is_fulfiller(actor):
    if not actor or actor.get("kind") != "agent":
        return False
    agent_id = actor.get("agent_id") or ""
    return bool(agent_id) and agent_id in fulfiller_ids()


def normalize_phone(value):
    if not isinstance(value, str):
        raise LuckinError("请填写中国大陆手机号")
    compact = re.sub(r"[\s\u3000\-]", "", value.strip())
    if compact.startswith("+86"):
        compact = compact[3:]
    elif compact.startswith("0086"):
        compact = compact[4:]
    elif compact.startswith("86") and len(compact) == 13:
        compact = compact[2:]
    if not re.fullmatch(r"1[3-9]\d{9}", compact):
        raise LuckinError("请填写中国大陆手机号")
    return compact


def mask_phone(digits):
    return digits[:3] + "****" + digits[-4:]


def normalize_email(value):
    if not isinstance(value, str):
        raise LuckinError("邮箱格式不正确")
    email = value.strip().lower()
    if len(email) > 254 or not re.fullmatch(
        r"[a-z0-9._%+\-]+@[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?)+",
        email,
    ):
        raise LuckinError("邮箱格式不正确")
    return email


def mask_email(email):
    local, domain = email.split("@", 1)
    shown = "*" if len(local) <= 1 else local[0] + "***" + local[-1]
    return shown + "@" + domain


def _clean(value, label, limit, required=True):
    if value is None or value == "":
        if required:
            raise LuckinError("请填写" + label)
        return ""
    if not isinstance(value, str):
        raise LuckinError(label + "不正确")
    text = " ".join(value.split())
    if required and not text:
        raise LuckinError("请填写" + label)
    if len(text) > limit:
        raise LuckinError(label + "请控制在" + str(limit) + "字以内")
    return text


def _coord(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LuckinError(label + "不正确")
    if not low <= float(value) <= high:
        raise LuckinError(label + "超出范围")
    return round(float(value), 6)


def shanghai_day(timestamp):
    return datetime.fromtimestamp(timestamp, SHANGHAI).date().isoformat()


def iso_shanghai(timestamp):
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, SHANGHAI).replace(microsecond=0).isoformat()


class Luckin:
    def __init__(self, root, public_base_path="", public_origin="", clock=None, poster=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "luckin.sqlite3"
        self.public_base_path = (public_base_path or "").rstrip("/")
        self.public_origin = (public_origin or "").rstrip("/")
        self.clock = clock or time.time
        self.poster = poster
        self.claim_ip_limit = 30
        self.claim_person_limit = 8
        self.read_ip_limit = 120
        self.rate_window = 600.0
        self.menu_ttl = MENU_TTL
        self._hits = {}
        self._lock = threading.Lock()
        self._menu_lock = threading.Lock()
        self._menu_cache = None
        self._key = None
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                  id TEXT PRIMARY KEY,
                  person_hash TEXT NOT NULL,
                  contact_kind TEXT NOT NULL,
                  contact_masked TEXT NOT NULL,
                  contact_sealed BLOB NOT NULL,
                  city TEXT NOT NULL,
                  shop_name TEXT NOT NULL,
                  location_note TEXT NOT NULL DEFAULT '',
                  lat REAL,
                  lng REAL,
                  drink_id TEXT NOT NULL,
                  drink_name TEXT NOT NULL,
                  product_id TEXT NOT NULL DEFAULT '',
                  sku TEXT NOT NULL DEFAULT '',
                  drink_spec TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL,
                  day TEXT NOT NULL,
                  staff_note TEXT NOT NULL DEFAULT '',
                  fail_reason TEXT NOT NULL DEFAULT '',
                  order_id TEXT NOT NULL DEFAULT '',
                  pickup_code TEXT NOT NULL DEFAULT '',
                  fulfill_shop TEXT NOT NULL DEFAULT '',
                  ready_hint TEXT NOT NULL DEFAULT '',
                  raw_note TEXT NOT NULL DEFAULT '',
                  created REAL NOT NULL,
                  updated REAL NOT NULL,
                  ready_at REAL,
                  result_token_hash TEXT NOT NULL UNIQUE,
                  agent_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS claims_day ON claims(day, status);
                CREATE INDEX IF NOT EXISTS claims_person ON claims(person_hash, day, status);
                """
            )
            self._migrate(db)
            db.execute("CREATE INDEX IF NOT EXISTS claims_agent ON claims(agent_id)")
        os.chmod(self.path, 0o600)

    def _migrate(self, db):
        columns = {row[1] for row in db.execute("PRAGMA table_info(claims)")}
        if "agent_id" not in columns:
            db.execute("ALTER TABLE claims ADD COLUMN agent_id TEXT NOT NULL DEFAULT ''")
        if "qr_url" not in columns:
            db.execute("ALTER TABLE claims ADD COLUMN qr_url TEXT NOT NULL DEFAULT ''")
        if "qr_data" not in columns:
            db.execute("ALTER TABLE claims ADD COLUMN qr_data TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _txn(self):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def limit(self, scope, key, limit):
        now = time.monotonic()
        bucket = scope + ":" + str(key)
        with self._lock:
            hits = [moment for moment in self._hits.get(bucket, ()) if now - moment < self.rate_window]
            if len(hits) >= limit:
                self._hits[bucket] = hits
                raise LuckinError("请求过于频繁，请稍后再试", 429, code="rate_limited")
            hits.append(now)
            self._hits[bucket] = hits
            if len(self._hits) > 2000:
                for name, values in list(self._hits.items()):
                    if not values or now - values[-1] >= self.rate_window:
                        self._hits.pop(name, None)

    def day(self, timestamp=None):
        return shanghai_day(self.clock() if timestamp is None else timestamp)

    def result_url(self, token, origin):
        base = (origin or self.public_origin or "").rstrip("/")
        return base + self.public_base_path + "/luckin/r/" + token

    def quota(self):
        day = self.day()
        with self._connect() as db:
            used = self._count(db, day)
        return {
            "day": day,
            "timezone": "Asia/Shanghai",
            "limit": DAILY_LIMIT,
            "perPerson": PERSON_LIMIT,
            "remaining": max(0, DAILY_LIMIT - used),
        }

    def forget_menu(self):
        with self._menu_lock:
            self._menu_cache = None

    def menu(self):
        """Drinks the account can order with a coupon. Empty when the service is down."""
        now = time.monotonic()
        with self._menu_lock:
            cached = self._menu_cache
            if cached is not None and cached[0] > now:
                return [dict(item) for item in cached[1]]
            drinks = self._load_menu()
            self._menu_cache = (now + self.menu_ttl, drinks)
            return [dict(item) for item in drinks]

    def menu_public(self):
        return [_public_drink(item) for item in self.menu()]

    def offer(self):
        """What an agent can be treated to right now, plus today's remaining cups."""
        try:
            drinks = self.menu_public()
        except Exception:
            drinks = []
            print("luckin menu offer failed", file=sys.stderr)
        quota = self.quota()
        return {
            "drinks": drinks,
            "remaining": quota["remaining"],
            "limit": quota["limit"],
            "perPerson": quota["perPerson"],
            "day": quota["day"],
            "timezone": quota["timezone"],
            "message": offer_message(drinks, quota["remaining"]),
        }

    def create(self, body, origin, *, agent_id=""):
        if not isinstance(body, dict):
            raise LuckinError("请求不正确")
        extra = sorted(set(body) - CLAIM_FIELDS)
        if extra:
            raise LuckinError("不支持这些字段：" + "、".join(extra))
        if body.get("consent") is not True:
            raise LuckinError("请先勾选同意：到店自取、每日限额，且这不是无条件福利")
        phone = body.get("phone") or ""
        email = body.get("email") or ""
        if phone:
            phone = normalize_phone(phone)
        if email:
            email = normalize_email(email)
        if phone:
            kind, contact, masked = "phone", phone, mask_phone(phone)
            key = "phone:" + phone
        elif email:
            kind, contact, masked = "email", email, mask_email(email)
            key = "email:" + email
        else:
            raise LuckinError("请填写手机号或邮箱")
        city = _clean(body.get("city"), "城市", 40)
        shop = _clean(body.get("shopName"), "店名", 80)
        note = _clean(body.get("locationNote"), "定位描述", 120, required=False)
        lat = body.get("lat", None)
        lng = body.get("lng", None)
        if lat is None and lng is None:
            lat_v = lng_v = None
        elif lat is None or lng is None:
            raise LuckinError("请同时填写纬度和经度，或都不填")
        else:
            lat_v = _coord(lat, "纬度", -90, 90)
            lng_v = _coord(lng, "经度", -180, 180)
        drink_id = body.get("drink_id") or body.get("drinkId") or body.get("drink") or ""
        if not isinstance(drink_id, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", drink_id.strip()):
            raise LuckinError("请从当前可请清单里选择饮品", code="drink_unavailable")
        drink_id = drink_id.strip()
        drinks = self.menu()
        if not drinks:
            raise LuckinError(NO_COUPONS, 409, code="no_coupons")
        drink = next((item for item in drinks if item["id"] == drink_id), None)
        if drink is None:
            raise LuckinError("这款饮品不在当前可请清单里。", 400, code="drink_unavailable")
        if agent_id is None:
            agent_id = ""
        if not isinstance(agent_id, str) or (agent_id and not re.fullmatch(r"ag_[0-9a-f]{24}", agent_id)):
            raise LuckinError("领取身份不正确", 500, code="server_error")
        self.limit("person", hashlib.sha256(key.encode()).hexdigest()[:16], self.claim_person_limit)
        now = self.clock()
        day = shanghai_day(now)
        claim_id = "lk_" + secrets.token_hex(8)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._txn() as db:
            salt = self._salt(db)
            person = hmac.new(salt.encode(), key.encode(), hashlib.sha256).hexdigest()
            if self._count(db, day) >= DAILY_LIMIT:
                raise LuckinError("今日两杯已送完，请明天再来。", 409, code="quota_full")
            if self._count(db, day, person) >= PERSON_LIMIT:
                raise LuckinError("今天已经领过一杯，请明天再来。", 409, code="already_claimed")
            sealed = self._seal(contact)
            db.execute(
                """INSERT INTO claims (
                    id, person_hash, contact_kind, contact_masked, contact_sealed,
                    city, shop_name, location_note, lat, lng,
                    drink_id, drink_name, product_id, sku, drink_spec,
                    status, day, created, updated, result_token_hash, agent_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    claim_id, person, kind, masked, sealed,
                    city, shop, note, lat_v, lng_v,
                    drink_id, drink["name"], drink.get("product_id") or "", drink.get("sku") or "", drink.get("spec") or "",
                    "ordering", day, now, now, token_hash, agent_id,
                ),
            )
        self._auto_order(claim_id, token, origin)
        with self._connect() as db:
            row = dict(db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone())
        shown = self._public(row, token=token, origin=origin)
        shown["message"] = _guest_message(shown)
        return shown

    def public_by_token(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{20,128}", token):
            raise LuckinError("没有这笔领取", 404, code="not_found")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            found = db.execute("SELECT * FROM claims WHERE result_token_hash=?", (digest,)).fetchone()
            row = dict(found) if found is not None else None
        if row is None:
            raise LuckinError("没有这笔领取", 404, code="not_found")
        return self._public(row)

    def claim_for_agent(self, agent_id, claim_id="", result_token="", origin=""):
        """Public view of one claim owned by this agent instance.

        HTML claims have no agent_id, so they stay on the result link only.
        A miss and someone else's claim use the same not-found error.
        """
        if not isinstance(agent_id, str) or not re.fullmatch(r"ag_[0-9a-f]{24}", agent_id):
            raise LuckinError("没有这笔领取", 404, code="not_found")
        has_id = claim_id not in (None, "")
        has_token = result_token not in (None, "")
        if not has_id and not has_token:
            raise LuckinError("请提供 claim_id 或 result_token")
        if has_id and (not isinstance(claim_id, str) or not re.fullmatch(r"lk_[0-9a-f]{16}", claim_id)):
            raise LuckinError("没有这笔领取", 404, code="not_found")
        if has_token and (not isinstance(result_token, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{20,128}", result_token)):
            raise LuckinError("没有这笔领取", 404, code="not_found")
        with self._connect() as db:
            if has_id:
                found = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            else:
                digest = hashlib.sha256(result_token.encode()).hexdigest()
                found = db.execute("SELECT * FROM claims WHERE result_token_hash=?", (digest,)).fetchone()
            row = dict(found) if found is not None else None
            if row is not None and has_id and has_token:
                digest = hashlib.sha256(result_token.encode()).hexdigest()
                if not hmac.compare_digest(row["result_token_hash"], digest):
                    row = None
            if row is None or not _owned(row.get("agent_id") or "", agent_id):
                raise LuckinError("没有这笔领取", 404, code="not_found")
        shown = self._public(row)
        if has_token:
            shown["resultUrl"] = self.result_url(result_token, origin)
        return shown

    def list_claims(self, status="pending"):
        if status not in STATUSES:
            raise LuckinError("status 不正确")
        with self._connect() as db:
            rows = [
                dict(item)
                for item in db.execute(
                    "SELECT * FROM claims WHERE status=? ORDER BY created ASC, id ASC LIMIT 100",
                    (status,),
                ).fetchall()
            ]
        return [self._fulfiller(row) for row in rows]

    def decide(self, claim_id, action, note=""):
        if action not in ("approve", "reject"):
            raise LuckinError("action 只能是 approve 或 reject")
        staff = _clean(note, "备注", 200, required=False) if note not in (None, "") else ""
        if note not in (None, "") and not isinstance(note, str):
            raise LuckinError("备注不正确")
        with self._txn() as db:
            row = self._locked(db, claim_id)
            status = row["status"]
            if action == "approve":
                if status == "ordering":
                    return self._fulfiller(dict(row))
                if status == "failed":
                    self._assert_retry(db, row)
                elif status != "pending":
                    raise LuckinError("这笔领取现在不能确认下单", 409, code="invalid_state")
                target = "ordering"
            else:
                if status == "rejected":
                    return self._fulfiller(dict(row))
                if status not in ("pending", "ordering"):
                    raise LuckinError("这笔领取现在不能拒绝", 409, code="invalid_state")
                target = "rejected"
            db.execute(
                """UPDATE claims SET status=?, staff_note=?,
                   fail_reason=CASE WHEN ?='ordering' THEN '' ELSE fail_reason END,
                   updated=? WHERE id=?""",
                (target, staff or row["staff_note"], target, self.clock(), claim_id),
            )
            row = dict(db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone())
        return self._fulfiller(row)

    def fulfill(self, claim_id, body):
        if not isinstance(body, dict):
            raise LuckinError("请求不正确")
        allowed = {
            "orderId", "order_id", "pickupCode", "pickup_code",
            "shopName", "shop_name", "storeName", "store_name",
            "readyHint", "ready_hint", "rawNote", "raw_note",
            "qrUrl", "qr_url", "qrData", "qr_data",
        }
        extra = sorted(set(body) - allowed)
        if extra:
            raise LuckinError("不支持这些字段：" + "、".join(extra))
        order_id = _clean(_first(body, "orderId", "order_id"), "订单号", 64)
        pickup = _clean(_first(body, "pickupCode", "pickup_code"), "取餐码", 64)
        shop = _clean(
            _first(body, "storeName", "store_name", "shopName", "shop_name", default=""),
            "门店", 80, required=False,
        )
        hint = _clean(_first(body, "readyHint", "ready_hint", default=""), "取餐提示", 200, required=False)
        raw = _clean(_first(body, "rawNote", "raw_note", default=""), "履约备注", 2000, required=False)
        qr_url = _pickup_qr_url(_first(body, "qrUrl", "qr_url", default="") or "")
        qr_data = _pickup_qr_data(_first(body, "qrData", "qr_data", default="") or "")
        return self._mark_ready(claim_id, order_id, pickup, shop, hint, raw, qr_url=qr_url, qr_data=qr_data)

    def fail(self, claim_id, reason):
        text = _clean(reason, "失败原因", 500)
        with self._txn() as db:
            row = self._locked(db, claim_id)
            if row["status"] == "failed":
                return self._fulfiller(dict(row))
            if row["status"] not in ("pending", "ordering"):
                raise LuckinError("这笔领取现在不能标为失败", 409, code="invalid_state")
            db.execute(
                "UPDATE claims SET status='failed', fail_reason=?, updated=? WHERE id=?",
                (text, self.clock(), claim_id),
            )
            row = dict(db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone())
        return self._fulfiller(row)

    def _assert_retry(self, db, row):
        """Move the same claim back to ordering. It is one cup, not a new claim."""
        if self._count(db, row["day"], exclude=row["id"]) >= DAILY_LIMIT:
            raise LuckinError("今日两杯已满，这笔失败单暂时不能拉回下单。", 409, code="quota_full")
        if self._count(db, row["day"], row["person_hash"], exclude=row["id"]) >= PERSON_LIMIT:
            raise LuckinError("这个人今天已经有一杯在排队，不能把失败单再拉回。", 409, code="already_claimed")

    def _count(self, db, day, person=None, exclude=None):
        sql = "SELECT COUNT(*) FROM claims WHERE day=? AND status IN ('pending','ordering','ready')"
        args = [day]
        if person is not None:
            sql += " AND person_hash=?"
            args.append(person)
        if exclude is not None:
            sql += " AND id!=?"
            args.append(exclude)
        return db.execute(sql, args).fetchone()[0]

    def _locked(self, db, claim_id):
        if not isinstance(claim_id, str) or not re.fullmatch(r"lk_[0-9a-f]{16}", claim_id):
            raise LuckinError("没有这笔领取", 404, code="not_found")
        row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None:
            raise LuckinError("没有这笔领取", 404, code="not_found")
        return row

    def _salt(self, db):
        row = db.execute("SELECT value FROM meta WHERE key='person_salt'").fetchone()
        if row is not None:
            return row["value"]
        salt = secrets.token_hex(32)
        db.execute("INSERT INTO meta(key, value) VALUES ('person_salt', ?)", (salt,))
        return salt

    def _contact_key(self):
        if self._key is not None:
            return self._key
        path = self.root / "luckin.key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = path.read_bytes()
        else:
            key = secrets.token_bytes(32)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
        os.chmod(path, 0o600)
        if len(key) != 32:
            raise LuckinError("联系方式密钥不可用", 500, code="server_error")
        self._key = key
        return key

    def _hide(self, text, contact, masked):
        if not text or not contact:
            return text or ""
        return text.replace(contact, masked)

    def _seal(self, text):
        key = self._contact_key()
        nonce = secrets.token_bytes(16)
        raw = text.encode()
        return nonce + _xor(key, nonce, raw)

    def _open(self, blob):
        key = self._contact_key()
        nonce, data = bytes(blob[:16]), bytes(blob[16:])
        return _xor(key, nonce, data).decode()

    def _public(self, row, token=None, origin=""):
        contact = self._open(row["contact_sealed"])
        masked = row["contact_masked"]
        shop = self._hide(row["fulfill_shop"] or row["shop_name"], contact, masked)
        shown = {
            "claimId": row["id"],
            "status": row["status"],
            "statusLabel": LABELS.get(row["status"], row["status"]),
            "drink": row["drink_name"],
            "city": self._hide(row["city"], contact, masked),
            "shopName": shop,
            "storeName": shop,
            "contactMasked": masked,
            "day": row["day"],
            "createdAt": iso_shanghai(row["created"]),
        }
        note = self._hide(row["location_note"], contact, masked)
        if note:
            shown["locationNote"] = note
        if row["status"] == "ready":
            if row["pickup_code"]:
                shown["pickupCode"] = row["pickup_code"]
            qr_url = row["qr_url"] if "qr_url" in row.keys() else ""
            qr_data = row["qr_data"] if "qr_data" in row.keys() else ""
            if qr_url:
                shown["qrUrl"] = qr_url
            if qr_data:
                shown["qrData"] = qr_data
            shown["orderTime"] = iso_shanghai(row["ready_at"])
            if row["ready_hint"]:
                shown["readyHint"] = row["ready_hint"]
        if row["status"] == "failed" and row["fail_reason"]:
            shown["failReason"] = self._hide(row["fail_reason"], contact, masked)
        if token:
            shown["resultToken"] = token
            shown["resultUrl"] = self.result_url(token, origin)
        return shown

    def _fulfiller(self, row):
        shown = {
            "claimId": row["id"],
            "status": row["status"],
            "statusLabel": LABELS.get(row["status"], row["status"]),
            "contact": self._open(row["contact_sealed"]),
            "contactKind": row["contact_kind"],
            "contactMasked": row["contact_masked"],
            "city": row["city"],
            "shopName": row["shop_name"],
            "locationNote": row["location_note"],
            "lat": row["lat"],
            "lng": row["lng"],
            "drink": row["drink_name"],
            "drinkId": row["drink_id"],
            "productId": row["product_id"],
            "sku": row["sku"],
            "drinkSpec": row["drink_spec"],
            "day": row["day"],
            "createdAt": iso_shanghai(row["created"]),
            "updatedAt": iso_shanghai(row["updated"]),
            "staffNote": row["staff_note"],
            "failReason": row["fail_reason"],
            "orderId": row["order_id"],
            "pickupCode": row["pickup_code"],
            "qrUrl": row["qr_url"] if "qr_url" in row.keys() else "",
            "storeName": row["fulfill_shop"] or row["shop_name"],
            "fulfillShop": row["fulfill_shop"],
            "readyHint": row["ready_hint"],
            "orderTime": iso_shanghai(row["ready_at"]),
        }
        if row["raw_note"]:
            shown["rawNote"] = row["raw_note"]
        return shown

    def _mark_ready(self, claim_id, order_id, pickup, shop, hint, raw, staff_note="", qr_url="", qr_data=""):
        note = ""
        if staff_note:
            note = _clean(staff_note, "备注", 200, required=False)
        if not pickup and not qr_url and not qr_data:
            raise LuckinError("缺少取餐码", code="api")
        with self._txn() as db:
            row = self._locked(db, claim_id)
            if row["status"] == "ready":
                if row["order_id"] == order_id and row["pickup_code"] == pickup:
                    return self._fulfiller(dict(row))
                raise LuckinError("这笔领取已经写过别的取餐码", 409, code="invalid_state")
            if row["status"] not in ("pending", "ordering"):
                raise LuckinError("这笔领取现在不能写入取餐码", 409, code="invalid_state")
            now = self.clock()
            db.execute(
                """UPDATE claims SET status='ready', order_id=?, pickup_code=?, fulfill_shop=?,
                   ready_hint=?, raw_note=?, qr_url=?, qr_data=?, ready_at=?, updated=?,
                   staff_note=CASE WHEN ?!='' THEN ? ELSE staff_note END
                   WHERE id=?""",
                (order_id, pickup, shop, hint, raw, qr_url, qr_data, now, now, note, note, claim_id),
            )
            row = dict(db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone())
        return self._fulfiller(row)

    def _auto_order(self, claim_id, token, origin):
        """Ask the media webhook to place the cup. Never persist its secrets."""
        with self._connect() as db:
            found = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            row = dict(found) if found is not None else None
        if row is None or row["status"] not in ("pending", "ordering"):
            return
        contact = self._open(row["contact_sealed"])
        masked = row["contact_masked"]
        try:
            payload = self._order_payload(row, token, origin, contact)
        except LuckinError:
            self._fail_quietly(claim_id, "自动下单参数不正确", contact, masked)
            return
        result = self._exchange(payload)
        if result.get("needPay") is True:
            reason = result.get("failReason") or result.get("reason") or "这杯需要另外支付，不能下单。"
            self._fail_quietly(claim_id, reason, contact, masked)
            return
        pickup = result.get("pickupCode") or ""
        qr_url = result.get("qrUrl") or ""
        qr_data = result.get("qrData") or ""
        if result.get("accepted") and not pickup and not qr_url and not qr_data:
            return
        if result.get("ok") is not False and (pickup or qr_url or qr_data):
            self._ready_quietly(claim_id, result, contact, masked)
            return
        if result.get("ok") and result.get("orderId"):
            return
        reason = result.get("failReason") or result.get("reason") or _FAIL_TEXT.get(result.get("code") or "", "自动下单失败")
        self._fail_quietly(claim_id, reason, contact, masked)

    def _ready_quietly(self, claim_id, result, contact, masked):
        shop = result.get("storeName") or result.get("shopName") or ""
        hint = result.get("readyHint") or "请到店出示取餐码或二维码"
        order_id = result.get("orderId") or claim_id
        pickup = result.get("pickupCode") or ""
        try:
            self._mark_ready(
                claim_id,
                _clean(self._safe_reason(order_id, contact, masked), "订单号", 64),
                _clean(self._safe_reason(pickup, contact, masked), "取餐码", 64, required=False) if pickup else "",
                _clean(self._safe_reason(shop, contact, masked), "门店", 80, required=False) if shop else "",
                _clean(self._safe_reason(hint, contact, masked), "取餐提示", 200, required=False),
                "",
                staff_note="auto:coupon",
                qr_url=result.get("qrUrl") or "",
                qr_data=result.get("qrData") or "",
            )
        except LuckinError:
            print("luckin auto ready failed claim=" + claim_id, file=sys.stderr)

    def _fail_quietly(self, claim_id, reason, contact, masked):
        try:
            text = _clean(self._safe_reason(reason, contact, masked), "失败原因", 500)
            self.fail(claim_id, text)
        except LuckinError:
            print("luckin auto fail failed claim=" + claim_id, file=sys.stderr)

    def _order_payload(self, row, token, origin, contact):
        masked = row["contact_masked"]
        payload = {
            "action": "place",
            "payWith": PAY_COUPON,
            "payPolicy": PAY_POLICY,
            "needPay": False,
            "claimId": row["id"],
            "resultUrl": self.result_url(token, origin),
            "contactMasked": masked,
            "city": self._hide(row["city"], contact, masked),
            "shopName": self._hide(row["shop_name"], contact, masked),
            "drink": row["drink_name"],
            "drinkId": row["drink_id"],
            "amount": 1,
            "spec": row["drink_spec"],
            "createdAt": iso_shanghai(row["created"]),
        }
        product_id = (row["product_id"] or "").strip()
        sku = row["sku"] or ""
        if product_id:
            try:
                product_value = int(product_id)
            except ValueError:
                product_value = product_id
            payload["productId"] = product_value
            payload["skuCode"] = sku
            payload["productList"] = [{"amount": 1, "productId": product_value, "skuCode": sku}]
        note = self._hide(row["location_note"], contact, masked)
        if note:
            payload["locationNote"] = note
        if row["lat"] is not None and row["lng"] is not None:
            payload["latitude"] = row["lat"]
            payload["longitude"] = row["lng"]
        leaked = set(payload) - ORDER_FIELDS
        blob = json.dumps(payload, ensure_ascii=False)
        if leaked or contact in blob:
            raise LuckinError("通知不能包含原始联系方式", 500, code="server_error")
        return payload

    def _load_menu(self):
        url = menu_webhook_url()
        if not url:
            if order_webhook_url() or os.environ.get("LUCKIN_MENU_WEBHOOK_URL", "").strip():
                print("luckin menu skipped: unsupported scheme", file=sys.stderr)
            return []
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            print("luckin menu skipped: unsupported scheme", file=sys.stderr)
            return []
        try:
            raw = self._post(url, {"action": "menu"}, MENU_TIMEOUT)
        except Exception:
            print("luckin menu failed", file=sys.stderr)
            return []
        return _menu_drinks(raw)

    def _exchange(self, payload):
        url = order_webhook_url()
        if not url:
            return {"ok": False, "code": "not_configured", "reason": "自动下单未配置"}
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            print("luckin webhook skipped: unsupported scheme", file=sys.stderr)
            return {"ok": False, "code": "not_configured", "reason": "自动下单地址不正确"}
        try:
            raw = self._post(url, payload, ORDER_TIMEOUT)
        except Exception:
            print("luckin webhook failed claim=" + str(payload.get("claimId", "")), file=sys.stderr)
            return {"ok": False, "code": "api", "reason": "自动下单通知失败"}
        return _order_response(raw)

    def _post(self, url, payload, timeout):
        if self.poster is not None:
            return self.poster(url, payload)
        return _post_json(url, payload, timeout=timeout)

    def _safe_reason(self, text, contact, masked):
        if not isinstance(text, str):
            return ""
        cleaned = self._hide(text, contact, masked)
        if re.search(r"coupon|token|secret|bearer", cleaned, re.I):
            return "下单失败"
        return cleaned


def _owned(stored, agent_id):
    if not stored or not agent_id or len(stored) != len(agent_id):
        return False
    return hmac.compare_digest(stored, agent_id)


def _first(body, *names, default=None):
    for name in names:
        if name in body and body[name] not in (None, ""):
            return body[name]
    return default


def _xor(key, nonce, data):
    out = bytearray()
    offset = 0
    counter = 0
    while offset < len(data):
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        chunk = data[offset:offset + 32]
        out.extend(byte ^ block[index] for index, byte in enumerate(chunk))
        offset += len(chunk)
        counter += 1
    return bytes(out)


def _guest_message(shown):
    if shown.get("status") == "ready":
        has_code = bool(shown.get("pickupCode"))
        has_qr = bool(shown.get("qrUrl") or shown.get("qrData"))
        if has_code and has_qr:
            return "已下单。取餐码和取餐二维码都在结果里，到店出示即可。"
        if has_qr:
            return "已下单。到店出示取餐二维码即可。"
        return "已下单。取餐码在结果里，到店报这个码即可。"
    if shown.get("status") == "failed":
        return shown.get("failReason") or "下单未成功，今日名额已释放。可以再提交一次。"
    return "正在下单。请保存结果链接，取餐码或二维码会写在这一页。"


def offer_message(drinks, remaining):
    if not drinks:
        return NO_COUPONS
    names = "、".join(item["name"] for item in drinks)
    return (
        "现在可以用券请：" + names + "。今日还剩 " + str(remaining)
        + " 杯。请选一杯，填取餐门店，再领取。下单成功会直接返回取餐码或取餐二维码。"
    )


_FAIL_TEXT = {
    "not_configured": "自动下单未配置",
    "no_coupon": "现在没有可用的券，暂时不能请客。",
    "shop": "没有匹配到可下单的门店",
    "api": "自动下单失败",
    "need_pay": "这杯需要另外支付，不能下单。",
}

_RESPONSE_KEYS = {
    "ok": "ok",
    "accepted": "accepted",
    "code": "code",
    "reason": "reason",
    "failreason": "failReason",
    "paywith": "payWith",
    "orderid": "orderId",
    "pickupcode": "pickupCode",
    "shopname": "shopName",
    "storename": "storeName",
    "readyhint": "readyHint",
    "needpay": "needPay",
    "qrurl": "qrUrl",
    "qrdata": "qrData",
}

_PAY_MARKERS = ("wxpay", "weixin://", "pay.weixin", "wx.tenpay", "mmpay", "alipay", "支付")


def _is_payment_artifact(value):
    if not isinstance(value, str):
        return False
    folded = value.casefold()
    return any(marker in folded for marker in _PAY_MARKERS)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
        return bool(int(value))
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
        return value.strip().lower() in ("true", "1", "yes")
    return None


def _public_drink(item):
    shown = {"id": item["id"], "name": item["name"], "spec": item.get("spec") or ""}
    if item.get("remaining") is not None:
        shown["remaining"] = item["remaining"]
    return shown


def _menu_drinks(data):
    if not isinstance(data, dict):
        return []
    raw_items = None
    for key in ("drinks", "menu", "items"):
        if isinstance(data.get(key), list):
            raw_items = data[key]
            break
    if raw_items is None:
        return []
    drinks = []
    seen = set()
    for item in raw_items:
        parsed = _menu_item(item)
        if parsed is None or parsed["id"] in seen:
            continue
        seen.add(parsed["id"])
        drinks.append(parsed)
    return drinks


def _menu_item(item):
    if not isinstance(item, dict):
        return None
    if _as_bool(item.get("needPay") if "needPay" in item else item.get("need_pay")) is True:
        return None
    drink_id = item.get("id") or item.get("drinkId") or item.get("drink_id")
    if not isinstance(drink_id, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", drink_id):
        return None
    known = DRINKS.get(drink_id) or {}
    name = item.get("name") if isinstance(item.get("name"), str) else ""
    name = " ".join(name.split()) or known.get("name") or ""
    if not name or len(name) > 40:
        return None
    spec = item.get("spec") if isinstance(item.get("spec"), str) else ""
    spec = " ".join(spec.split())[:120] or known.get("spec") or ""
    remaining = item.get("remaining", item.get("count"))
    if remaining in (None, ""):
        remaining_v = None
    elif isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or isinstance(remaining, float) and not remaining.is_integer():
        return None
    else:
        remaining_v = int(remaining)
        if remaining_v <= 0:
            return None
    product_id = item.get("productId") or item.get("product_id") or known.get("product_id") or ""
    sku = item.get("sku") or item.get("skuCode") or item.get("sku_code") or known.get("sku") or ""
    return {
        "id": drink_id,
        "name": name,
        "spec": spec,
        "remaining": remaining_v,
        "product_id": str(product_id).strip()[:32],
        "sku": str(sku).strip()[:64],
    }


def _pickup_qr_url(value):
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or len(text) > 500 or _is_payment_artifact(text):
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    return text


def _pickup_qr_data(value):
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > 24000 or _is_payment_artifact(text):
        return ""
    if text.startswith("data:image/") and ";base64," in text[:80]:
        return text
    return ""


_PAY_KEYS = {
    "payurl", "payqr", "payqrcode", "paymenturl", "paymentqr", "codeurl",
    "mweburl", "wechatpay", "wxpayurl", "wxpay", "paylink",
}


def _order_response(data):
    """Keep only order fields. Coupon codes, tokens, and payment QR links never leave this function."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {"ok": False, "code": "api", "reason": "自动下单返回无法识别"}
    payment_field = False
    for key, value in data.items():
        folded = str(key).replace("_", "").lower()
        if folded in _PAY_KEYS and value not in (None, "", False):
            payment_field = True
        elif isinstance(value, str) and folded not in ("readyhint", "reason", "failreason", "shopname", "storename") and _is_payment_artifact(value):
            payment_field = True
    out = {}
    for key, value in data.items():
        folded = str(key).replace("_", "").lower()
        mapped = _RESPONSE_KEYS.get(folded)
        if mapped is None:
            mapped = _RESPONSE_KEYS.get(str(key).lower())
        if mapped:
            out[mapped] = value
    if "ok" in out:
        out["ok"] = out["ok"] is True
    if "accepted" in out:
        out["accepted"] = out["accepted"] is True
    need_pay = _as_bool(out.get("needPay")) if "needPay" in out else None
    if need_pay is not None:
        out["needPay"] = need_pay
    else:
        out.pop("needPay", None)
    for key in ("reason", "failReason"):
        if key not in out:
            continue
        if not isinstance(out[key], str):
            out.pop(key)
        else:
            out[key] = " ".join(out[key].split())[:500]
    if "code" in out:
        if not isinstance(out["code"], str) or not re.fullmatch(r"[a-z0-9_]{1,40}", out["code"]):
            out.pop("code", None)
    for key in ("orderId", "pickupCode", "shopName", "storeName", "readyHint", "payWith"):
        if key not in out:
            continue
        value = out[key]
        if isinstance(value, bool) or value is None:
            out.pop(key)
        elif isinstance(value, int):
            out[key] = str(value)
        elif isinstance(value, float) and value.is_integer():
            out[key] = str(int(value))
        elif isinstance(value, str):
            out[key] = " ".join(value.split())
        else:
            out.pop(key)
    if out.get("needPay") is not True:
        if "qrUrl" in out:
            cleaned = _pickup_qr_url(out["qrUrl"])
            if cleaned:
                out["qrUrl"] = cleaned
            else:
                out.pop("qrUrl", None)
        if "qrData" in out:
            cleaned = _pickup_qr_data(out["qrData"])
            if cleaned:
                out["qrData"] = cleaned
            else:
                out.pop("qrData", None)
        if payment_field and not out.get("pickupCode") and not out.get("qrUrl") and not out.get("qrData"):
            out["needPay"] = True
    if out.get("needPay") is True:
        out.pop("qrUrl", None)
        out.pop("qrData", None)
        out.pop("pickupCode", None)
        out.setdefault("failReason", "这杯需要另外支付，不能下单。")
    return out


def _post_json(url, payload, timeout=ORDER_TIMEOUT):
    data = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers=webhook_headers(data, webhook_secret(), int(time.time())),
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(65536)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("order webhook returned an unreadable body") from exc
    if not isinstance(parsed, dict):
        return {}
    return parsed
