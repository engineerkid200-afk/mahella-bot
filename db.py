import asyncio
import functools
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from util import norm, ncode

DB_PATH = os.getenv("DB_PATH") or ("/data/mahla.db" if os.path.isdir("/data") else "mahla.db")
STATUSES = ["در انتظار بررسی", "در حال آماده‌سازی", "ارسال شده", "تحویل داده شده", "لغو شده"]
CANCELLED = STATUSES[-1]
DONE = (STATUSES[3], STATUSES[4])
DEFAULT_STOCK = 100
IRAN = timezone(timedelta(hours=3, minutes=30))

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")
_conn.execute("PRAGMA busy_timeout=5000")
_lock = threading.Lock()


def now():
    return datetime.now(IRAN).strftime("%Y-%m-%d %H:%M")


def _all(sql, a=()):
    return [dict(r) for r in _conn.execute(sql, a).fetchall()]


def _one(sql, a=()):
    r = _conn.execute(sql, a).fetchone()
    return dict(r) if r else None


def _ex(sql, a=()):
    return _conn.execute(sql, a)


def _locked(fn, a, k):
    with _lock:
        return fn(*a, **k)


def aio(fn):
    """تابع همگام را در ترد جدا و با قفل اجرا می‌کند تا ربات هنگام کار با دیتابیس بلاک نشود."""
    @functools.wraps(fn)
    async def wrapper(*a, **k):
        return await asyncio.to_thread(_locked, fn, a, k)
    return wrapper


SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  brand TEXT DEFAULT '', category TEXT DEFAULT 'سایر', description TEXT DEFAULT '',
  price INTEGER DEFAULT 0, stock INTEGER DEFAULT 0, photo TEXT, extra TEXT DEFAULT '{}',
  search TEXT DEFAULT '', created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_products_cat ON products(category);
CREATE TABLE IF NOT EXISTS cart(user_id INTEGER, product_id INTEGER, qty INTEGER, PRIMARY KEY(user_id, product_id));
CREATE TABLE IF NOT EXISTS cart_meta(user_id INTEGER PRIMARY KEY, coupon TEXT);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, name TEXT, items TEXT,
  subtotal INTEGER, discount INTEGER, discount_note TEXT, shipping INTEGER, total INTEGER,
  coupon TEXT, lat REAL, lon REAL, status TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE TABLE IF NOT EXISTS coupons(code TEXT PRIMARY KEY, percent INTEGER, max_uses INTEGER DEFAULT 0, used INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS tiers(min_amount INTEGER PRIMARY KEY, percent INTEGER);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, user_id INTEGER, rating INTEGER,
  comment TEXT, created_at TEXT, UNIQUE(product_id, user_id));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, name TEXT, first_seen TEXT, active INTEGER DEFAULT 1);
"""


def _import_old_users():
    """اگر دیتابیس نسخه‌ی قبلی (shop.db) کنار ربات باشد، فقط لیست کاربران را برای پیام همگانی منتقل می‌کند."""
    if _one("SELECT COUNT(*) c FROM users")["c"]:
        return
    for path in ("/data/shop.db", "shop.db"):
        if os.path.exists(path) and os.path.abspath(path) != os.path.abspath(DB_PATH):
            try:
                old = sqlite3.connect(path)
                for (uid,) in old.execute("SELECT user_id FROM users").fetchall():
                    _ex("INSERT OR IGNORE INTO users(user_id,name,first_seen,active) VALUES(?,?,?,1)", (uid, "", now()))
                old.close()
            except sqlite3.Error:
                pass


def init_sync():
    with _lock:
        _conn.executescript(SCHEMA)
        for k, v in (("shipping_fee", "0"), ("low_stock", "5"), ("channels", "[]")):
            _ex("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        _import_old_users()


# ---------- settings ----------
def _get(k, d=None):
    r = _one("SELECT value FROM settings WHERE key=?", (k,))
    return r["value"] if r else d


@aio
def get_setting(k, d=None):
    return _get(k, d)


@aio
def set_setting(k, v):
    _ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))


def _int_setting(k, d=0):
    try:
        return int(_get(k, str(d)))
    except (TypeError, ValueError):
        return d


# ---------- products ----------
def _st(m):
    return norm(" ".join(str(m.get(k) or "") for k in ("code", "name", "brand", "category", "description")))


def _next_code():
    n = _one("SELECT COALESCE(MAX(id),0) m FROM products")["m"] + 1
    while _one("SELECT 1 x FROM products WHERE code=?", (f"M{n:04d}",)):
        n += 1
    return f"M{n:04d}"


def _upsert(p):
    """None یعنی: مقدار قبلی را نگه دار (برای ردیف جدید مقدار پیش‌فرض)."""
    ex = _one("SELECT * FROM products WHERE code=?", (p["code"],))
    if ex:
        m = dict(ex)
        for k in ("name", "brand", "category", "description", "price", "stock", "photo"):
            if p.get(k) is not None:
                m[k] = p[k]
        extra = json.loads(ex["extra"] or "{}")
        extra.update(p.get("extra") or {})
        m["extra"] = json.dumps(extra, ensure_ascii=False)
        m["search"] = _st(m)
        _ex("UPDATE products SET name=?,brand=?,category=?,description=?,price=?,stock=?,photo=?,extra=?,search=? WHERE id=?",
            (m["name"], m["brand"], m["category"], m["description"], m["price"], m["stock"], m["photo"], m["extra"], m["search"], m["id"]))
        return "updated", m["id"]
    m = {
        "code": p["code"], "name": p["name"], "brand": p.get("brand") or "",
        "category": p.get("category") or "سایر", "description": p.get("description") or "",
        "price": p.get("price") or 0, "stock": DEFAULT_STOCK if p.get("stock") is None else p["stock"],
        "photo": p.get("photo"), "extra": json.dumps(p.get("extra") or {}, ensure_ascii=False),
    }
    m["search"] = _st(m)
    cur = _ex("INSERT INTO products(code,name,brand,category,description,price,stock,photo,extra,search,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (m["code"], m["name"], m["brand"], m["category"], m["description"], m["price"], m["stock"], m["photo"], m["extra"], m["search"], now()))
    return "added", cur.lastrowid


def _delete_product(pid):
    _ex("DELETE FROM cart WHERE product_id=?", (pid,))
    _ex("DELETE FROM reviews WHERE product_id=?", (pid,))
    _ex("DELETE FROM products WHERE id=?", (pid,))


@aio
def bulk_upsert(items, replace=False):
    added = updated = removed = 0
    _ex("BEGIN IMMEDIATE")
    try:
        by_full, by_name = {}, {}
        for r in _all("SELECT code,name,brand FROM products"):
            by_full[norm(r["name"]) + "|" + norm(r["brand"])] = r["code"]
            by_name.setdefault(norm(r["name"]), r["code"])
        seen = set()
        for p in items:
            if not p.get("code"):
                nn = norm(p["name"])
                code = by_full.get(nn + "|" + norm(p.get("brand") or ""))
                if not code and p.get("brand") is None:
                    code = by_name.get(nn)
                if not code:
                    code = _next_code()
                p = dict(p, code=code)
            res, _ = _upsert(p)
            added += res == "added"
            updated += res == "updated"
            seen.add(p["code"])
            nn = norm(p["name"])
            by_full[nn + "|" + norm(p.get("brand") or "")] = p["code"]
            by_name.setdefault(nn, p["code"])
        if replace and seen:
            for row in _all("SELECT id,code FROM products"):
                if row["code"] not in seen:
                    _delete_product(row["id"])
                    removed += 1
        _ex("COMMIT")
    except Exception:
        _ex("ROLLBACK")
        raise
    return {"added": added, "updated": updated, "removed": removed}


@aio
def save_product(p):
    if not p.get("code"):
        p = dict(p, code=_next_code())
    res, pid = _upsert(p)
    return res, _one("SELECT * FROM products WHERE id=?", (pid,))


@aio
def get_product(pid):
    return _one("SELECT * FROM products WHERE id=?", (pid,))


@aio
def update_field(pid, field, value):
    if field not in ("name", "brand", "category", "description", "price", "stock", "photo", "code"):
        return "bad"
    try:
        _ex(f"UPDATE products SET {field}=? WHERE id=?", (value, pid))
    except sqlite3.IntegrityError:
        return "dup"
    m = _one("SELECT * FROM products WHERE id=?", (pid,))
    if m:
        _ex("UPDATE products SET search=? WHERE id=?", (_st(m), pid))
    return "ok"


@aio
def delete_product(pid):
    _delete_product(pid)


@aio
def set_photo(code, file_ref):
    return _ex("UPDATE products SET photo=? WHERE code=?", (file_ref, ncode(code))).rowcount


@aio
def categories():
    return [(r["category"], r["c"]) for r in _all("SELECT category, COUNT(*) c FROM products GROUP BY category ORDER BY category")]


@aio
def products_in(cat):
    return _all("SELECT * FROM products WHERE category=? ORDER BY (stock>0) DESC, name", (cat,))


@aio
def all_products():
    return _all("SELECT * FROM products ORDER BY category, name")


@aio
def count_products():
    return _one("SELECT COUNT(*) c FROM products")["c"]


@aio
def search(q, lim=20):
    exact = _all("SELECT * FROM products WHERE code=?", (ncode(q),))
    if exact:
        return exact
    words = norm(q).split()
    if not words:
        return []
    where = " AND ".join(["search LIKE ?"] * len(words))
    return _all(f"SELECT * FROM products WHERE {where} ORDER BY (stock>0) DESC, name LIMIT ?", [f"%{w}%" for w in words] + [lim])


# ---------- cart ----------
@aio
def cart_add(uid, pid, qty):
    p = _one("SELECT stock FROM products WHERE id=?", (pid,))
    if not p or p["stock"] <= 0:
        return None
    cur = _one("SELECT qty FROM cart WHERE user_id=? AND product_id=?", (uid, pid))
    old = cur["qty"] if cur else 0
    new = min(old + qty, p["stock"])
    _ex("INSERT INTO cart(user_id,product_id,qty) VALUES(?,?,?) ON CONFLICT(user_id,product_id) DO UPDATE SET qty=excluded.qty", (uid, pid, new))
    return {"qty": new, "added": new - old, "capped": old + qty > p["stock"]}


@aio
def cart_set(uid, pid, qty):
    """تعداد را تنظیم می‌کند (حداکثر تا موجودی). خروجی: (تعداد نهایی، آیا به سقف موجودی خورد)"""
    p = _one("SELECT stock FROM products WHERE id=?", (pid,))
    if qty <= 0 or not p:
        _ex("DELETE FROM cart WHERE user_id=? AND product_id=?", (uid, pid))
        return 0, False
    capped = qty > p["stock"]
    qty = min(qty, p["stock"])
    if qty <= 0:
        _ex("DELETE FROM cart WHERE user_id=? AND product_id=?", (uid, pid))
        return 0, True
    _ex("INSERT INTO cart(user_id,product_id,qty) VALUES(?,?,?) ON CONFLICT(user_id,product_id) DO UPDATE SET qty=excluded.qty", (uid, pid, qty))
    return qty, capped


@aio
def cart_qty(uid, pid):
    r = _one("SELECT qty FROM cart WHERE user_id=? AND product_id=?", (uid, pid))
    return r["qty"] if r else 0


@aio
def cart_clear(uid):
    _ex("DELETE FROM cart WHERE user_id=?", (uid,))
    _ex("DELETE FROM cart_meta WHERE user_id=?", (uid,))


def _cart_items(uid):
    return _all("SELECT c.product_id pid, c.qty, p.code, p.name, p.price, p.stock FROM cart c "
                "JOIN products p ON p.id=c.product_id WHERE c.user_id=? ORDER BY c.rowid", (uid,))


def _coupon(code):
    if not code:
        return None
    cp = _one("SELECT * FROM coupons WHERE code=?", (code,))
    if cp and cp["active"] and (cp["max_uses"] == 0 or cp["used"] < cp["max_uses"]):
        return cp
    return None


def _totals(items, coupon_code):
    sub = sum(i["price"] * i["qty"] for i in items)
    cp = _coupon(coupon_code)
    cpct = cp["percent"] if cp else 0
    tier = _one("SELECT * FROM tiers WHERE min_amount<=? ORDER BY percent DESC LIMIT 1", (sub,))
    tpct = tier["percent"] if tier else 0
    pct = max(cpct, tpct)
    kind = None
    note = ""
    if pct > 0:
        kind = "coupon" if cpct >= tpct else "tier"
        note = f"کوپن {cp['code']} ({cpct}٪)" if kind == "coupon" else f"تخفیف پله‌ای ({tpct}٪)"
    disc = sub * pct // 100
    ship = _int_setting("shipping_fee", 0) if sub > 0 else 0
    nxt = _one("SELECT * FROM tiers WHERE min_amount>? ORDER BY min_amount ASC LIMIT 1", (sub,))
    return {"items": items, "subtotal": sub, "coupon": cp["code"] if cp else None, "pct": pct, "kind": kind,
            "note": note, "discount": disc, "shipping": ship, "total": max(0, sub - disc + ship), "next_tier": nxt}


@aio
def cart_summary(uid):
    meta = _one("SELECT coupon FROM cart_meta WHERE user_id=?", (uid,))
    return _totals(_cart_items(uid), meta["coupon"] if meta else None)


@aio
def apply_coupon(uid, code):
    cp = _coupon(ncode(code))
    if not cp:
        return None
    _ex("INSERT INTO cart_meta(user_id,coupon) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET coupon=excluded.coupon", (uid, cp["code"]))
    return cp


@aio
def remove_coupon(uid):
    _ex("DELETE FROM cart_meta WHERE user_id=?", (uid,))


# ---------- orders ----------
@aio
def create_order(uid, username, name, lat, lon):
    """ثبت سفارش به‌صورت اتمیک: بررسی موجودی، کم کردن موجودی، مصرف کوپن و خالی کردن سبد."""
    _ex("BEGIN IMMEDIATE")
    try:
        items = _cart_items(uid)
        if not items:
            _ex("ROLLBACK")
            return {"status": "empty"}
        short = [i for i in items if i["stock"] < i["qty"]]
        if short:
            _ex("ROLLBACK")
            return {"status": "stock", "items": short}
        meta = _one("SELECT coupon FROM cart_meta WHERE user_id=?", (uid,))
        t = _totals(items, meta["coupon"] if meta else None)
        snap = [{"pid": i["pid"], "code": i["code"], "name": i["name"], "qty": i["qty"], "price": i["price"]} for i in items]
        cur = _ex("INSERT INTO orders(user_id,username,name,items,subtotal,discount,discount_note,shipping,total,coupon,lat,lon,status,created_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (uid, username, name, json.dumps(snap, ensure_ascii=False), t["subtotal"], t["discount"], t["note"],
                   t["shipping"], t["total"], t["coupon"] if t["kind"] == "coupon" else "", lat, lon, STATUSES[0], now()))
        oid = cur.lastrowid
        for i in items:
            _ex("UPDATE products SET stock=stock-? WHERE id=?", (i["qty"], i["pid"]))
        if t["kind"] == "coupon":
            _ex("UPDATE coupons SET used=used+1 WHERE code=?", (t["coupon"],))
        _ex("DELETE FROM cart WHERE user_id=?", (uid,))
        _ex("DELETE FROM cart_meta WHERE user_id=?", (uid,))
        low_th = _int_setting("low_stock", 5)
        ids = [i["pid"] for i in items]
        low = _all(f"SELECT id,code,name,stock FROM products WHERE stock<=? AND id IN ({','.join('?' * len(ids))})", [low_th] + ids)
        order = _one("SELECT * FROM orders WHERE id=?", (oid,))
        _ex("COMMIT")
    except Exception:
        _ex("ROLLBACK")
        raise
    return {"status": "ok", "order": order, "low": low}


@aio
def get_order(oid):
    return _one("SELECT * FROM orders WHERE id=?", (oid,))


@aio
def user_orders(uid, lim=10):
    return _all("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, lim))


@aio
def admin_orders(lim=20):
    return _all("SELECT * FROM orders ORDER BY (status IN (?,?)) ASC, id DESC LIMIT ?", (DONE[0], DONE[1], lim))


@aio
def set_order_status(oid, status):
    o = _one("SELECT * FROM orders WHERE id=?", (oid,))
    if not o:
        return None
    if o["status"] == CANCELLED or o["status"] == status:
        return dict(o, changed=False)
    _ex("BEGIN IMMEDIATE")
    try:
        _ex("UPDATE orders SET status=? WHERE id=?", (status, oid))
        if status == CANCELLED:
            for it in json.loads(o["items"]):
                _ex("UPDATE products SET stock=stock+? WHERE id=?", (it["qty"], it["pid"]))
            if o["coupon"]:
                _ex("UPDATE coupons SET used=MAX(0,used-1) WHERE code=?", (o["coupon"],))
        _ex("COMMIT")
    except Exception:
        _ex("ROLLBACK")
        raise
    o["status"] = status
    o["changed"] = True
    return o


# ---------- coupons / tiers ----------
@aio
def add_coupon(code, percent, max_uses=0):
    _ex("INSERT INTO coupons(code,percent,max_uses,used,active) VALUES(?,?,?,0,1) "
        "ON CONFLICT(code) DO UPDATE SET percent=excluded.percent,max_uses=excluded.max_uses,active=1", (code, percent, max_uses))


@aio
def del_coupon(code):
    _ex("DELETE FROM coupons WHERE code=?", (code,))


@aio
def list_coupons():
    return _all("SELECT * FROM coupons ORDER BY code")


@aio
def add_tier(min_amount, percent):
    _ex("INSERT INTO tiers(min_amount,percent) VALUES(?,?) ON CONFLICT(min_amount) DO UPDATE SET percent=excluded.percent", (min_amount, percent))


@aio
def del_tier(min_amount):
    _ex("DELETE FROM tiers WHERE min_amount=?", (min_amount,))


@aio
def list_tiers():
    return _all("SELECT * FROM tiers ORDER BY min_amount")


# ---------- reviews ----------
@aio
def add_rating(pid, uid, rating):
    _ex("INSERT INTO reviews(product_id,user_id,rating,created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(product_id,user_id) DO UPDATE SET rating=excluded.rating, created_at=excluded.created_at", (pid, uid, rating, now()))


@aio
def add_comment(pid, uid, text):
    _ex("UPDATE reviews SET comment=? WHERE product_id=? AND user_id=?", (text, pid, uid))


@aio
def rating(pid):
    r = _one("SELECT AVG(rating) a, COUNT(*) c FROM reviews WHERE product_id=?", (pid,))
    return (round(r["a"], 1) if r["a"] else 0, r["c"])


@aio
def recent_comments(pid, lim=5):
    return _all("SELECT rating, comment, created_at FROM reviews WHERE product_id=? AND comment IS NOT NULL AND comment!='' ORDER BY id DESC LIMIT ?", (pid, lim))


# ---------- users / stats ----------
@aio
def track_user(uid, name):
    _ex("INSERT INTO users(user_id,name,first_seen,active) VALUES(?,?,?,1) ON CONFLICT(user_id) DO UPDATE SET active=1,name=excluded.name", (uid, name, now()))


@aio
def user_ids():
    return [r["user_id"] for r in _all("SELECT user_id FROM users WHERE active=1")]


@aio
def deactivate_user(uid):
    _ex("UPDATE users SET active=0 WHERE user_id=?", (uid,))


@aio
def stats():
    return {
        "products": _one("SELECT COUNT(*) c FROM products")["c"],
        "users": _one("SELECT COUNT(*) c FROM users WHERE active=1")["c"],
        "orders": _one("SELECT COUNT(*) c FROM orders")["c"],
        "pending": _one("SELECT COUNT(*) c FROM orders WHERE status=?", (STATUSES[0],))["c"],
        "revenue": _one("SELECT COALESCE(SUM(total),0) s FROM orders WHERE status!=?", (CANCELLED,))["s"],
        "out": _one("SELECT COUNT(*) c FROM products WHERE stock<=0")["c"],
    }
