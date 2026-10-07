"""لایه‌ی دیتابیس ربات ماهلا (SQLite).

نسخه‌ی پیشرفته:
  • جدول ادمین‌ها (چند ادمین، افزودن/حذف با آی‌دی عددی، مالک اصلی همیشه ثابت)
  • حذف دسته‌جمعی محصولات (همه / یک دسته / ناموجودها / انتخابی)
  • کوپن پیشرفته: درصدی یا مبلغ ثابت، حداقل خرید، تاریخ انقضا، سقف استفاده،
    یک‌بار برای هر کاربر، فعال/غیرفعال، حذف همه، حذف منقضی‌ها
  • تغییر قیمت گروهی (درصدی)، پشتیبان‌گیری از دیتابیس، آمار کامل‌تر
  • مهاجرت خودکار: دیتابیس قدیمی بدون از دست رفتن اطلاعات به نسخه‌ی جدید ارتقا می‌یابد.

نکته: همه‌ی توابع قبلی با همان نام و امضا کار می‌کنند، پس bot.py فعلی بدون تغییر هم بالا می‌آید.
"""
import asyncio
import functools
import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone

from util import money, ncode, norm

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
_lock = threading.RLock()

_OWNER = None          # آی‌دی مالک اصلی (از OWNER_ID)
_ADMINS = frozenset()  # کش آی‌دی ادمین‌ها برای بررسی سریع و همگام


def now():
    return datetime.now(IRAN).strftime("%Y-%m-%d %H:%M")


def today():
    return datetime.now(IRAN).strftime("%Y-%m-%d")


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


def _tx(fn):
    """تابع را داخل یک تراکنش اتمیک اجرا می‌کند؛ با خطا همه‌چیز برمی‌گردد."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        _ex("BEGIN IMMEDIATE")
        try:
            r = fn(*a, **k)
            _ex("COMMIT")
            return r
        except Exception:
            _ex("ROLLBACK")
            raise
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
CREATE TABLE IF NOT EXISTS coupon_uses(code TEXT, user_id INTEGER, order_id INTEGER, PRIMARY KEY(code, order_id));
CREATE INDEX IF NOT EXISTS idx_coupon_uses ON coupon_uses(code, user_id);
CREATE TABLE IF NOT EXISTS tiers(min_amount INTEGER PRIMARY KEY, percent INTEGER);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, user_id INTEGER, rating INTEGER,
  comment TEXT, created_at TEXT, UNIQUE(product_id, user_id));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, name TEXT, first_seen TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, name TEXT DEFAULT '', role TEXT DEFAULT 'admin', added_at TEXT);
"""

# ستون‌هایی که در نسخه‌ی جدید به جدول‌های قدیمی اضافه می‌شوند
MIGRATIONS = [
    ("coupons", "fixed", "INTEGER DEFAULT 0"),          # تخفیف مبلغ ثابت (تومان)
    ("coupons", "min_amount", "INTEGER DEFAULT 0"),     # حداقل مبلغ سبد
    ("coupons", "expires_at", "TEXT"),                  # تاریخ انقضا YYYY-MM-DD (میلادی)
    ("coupons", "one_per_user", "INTEGER DEFAULT 0"),   # هر کاربر فقط یک بار
    ("coupons", "created_at", "TEXT"),
]


def _columns(table):
    return {r["name"] for r in _all(f"PRAGMA table_info({table})")}


def _migrate():
    for table, col, ddl in MIGRATIONS:
        if col not in _columns(table):
            _ex(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


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


def init_sync(owner_id=None):
    """راه‌اندازی دیتابیس. owner_id = آی‌دی مالک اصلی که همیشه ادمین است و حذف نمی‌شود."""
    global _OWNER
    with _lock:
        _conn.executescript(SCHEMA)
        _migrate()
        for k, v in (("shipping_fee", "0"), ("low_stock", "5"), ("channels", "[]")):
            _ex("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        if owner_id:
            _OWNER = int(owner_id)
            _ex("INSERT INTO admins(user_id,name,role,added_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET role='owner'", (_OWNER, "", "owner", now()))
        _reload_admins()
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


# ---------- admins ----------
def _reload_admins():
    global _ADMINS
    ids = {r["user_id"] for r in _all("SELECT user_id FROM admins")}
    if _OWNER:
        ids.add(_OWNER)
    _ADMINS = frozenset(ids)


def is_admin(uid):
    """همگام و سریع (از کش). مالک اصلی و همه‌ی ادمین‌های ثبت‌شده True می‌گیرند."""
    return uid is not None and int(uid) in _ADMINS


def is_owner(uid):
    """فقط مالک اصلی (برای مدیریت ادمین‌ها)."""
    return uid is not None and _OWNER is not None and int(uid) == _OWNER


def admin_ids_sync():
    return sorted(_ADMINS)


@aio
def admin_ids():
    return sorted(_ADMINS)


@aio
def list_admins():
    rows = _all("SELECT * FROM admins ORDER BY (role='owner') DESC, added_at")
    if _OWNER and not any(r["user_id"] == _OWNER for r in rows):
        rows.insert(0, {"user_id": _OWNER, "name": "", "role": "owner", "added_at": ""})
    return rows


@aio
def add_admin(uid, name=""):
    """خروجی: 'added' | 'exists' | 'owner'"""
    uid = int(uid)
    if uid == _OWNER:
        return "owner"
    ex = _one("SELECT 1 x FROM admins WHERE user_id=?", (uid,))
    _ex("INSERT INTO admins(user_id,name,role,added_at) VALUES(?,?,'admin',?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=CASE WHEN excluded.name!='' THEN excluded.name ELSE admins.name END",
        (uid, name or "", now()))
    _reload_admins()
    return "exists" if ex else "added"


@aio
def remove_admin(uid):
    """خروجی: 'removed' | 'missing' | 'owner' (مالک اصلی قابل حذف نیست)"""
    uid = int(uid)
    if uid == _OWNER:
        return "owner"
    n = _ex("DELETE FROM admins WHERE user_id=? AND role!='owner'", (uid,)).rowcount
    _reload_admins()
    return "removed" if n else "missing"


@aio
def replace_admins(new_ids):
    """همه‌ی ادمین‌های فرعی را با لیست جدید جایگزین می‌کند (مالک اصلی دست‌نخورده می‌ماند)."""
    ids = {int(i) for i in new_ids if int(i) != _OWNER}
    _ex("BEGIN IMMEDIATE")
    try:
        _ex("DELETE FROM admins WHERE role!='owner'")
        for i in ids:
            _ex("INSERT INTO admins(user_id,name,role,added_at) VALUES(?,?,'admin',?)", (i, "", now()))
        _ex("COMMIT")
    except Exception:
        _ex("ROLLBACK")
        raise
    _reload_admins()
    return len(ids)


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


def _delete_where(where, args=()):
    """حذف گروهی با شرط؛ سبدها و نظرات مرتبط هم پاک می‌شوند. خروجی: تعداد حذف‌شده."""
    sub = f"SELECT id FROM products WHERE {where}"
    n = _one(f"SELECT COUNT(*) c FROM products WHERE {where}", args)["c"]
    if n:
        _ex(f"DELETE FROM cart WHERE product_id IN ({sub})", args)
        _ex(f"DELETE FROM reviews WHERE product_id IN ({sub})", args)
        _ex(f"DELETE FROM products WHERE {where}", args)
    return n


@aio
@_tx
def bulk_upsert(items, replace=False):
    added = updated = removed = 0
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


# --- حذف دسته‌جمعی ---
@aio
@_tx
def delete_all_products():
    """همه‌ی محصولات را یک‌جا حذف می‌کند (سبدها و نظرات هم پاک می‌شوند). خروجی: تعداد."""
    return _delete_where("1=1")


@aio
@_tx
def delete_category(cat):
    """همه‌ی محصولات یک دسته. خروجی: تعداد."""
    return _delete_where("category=?", (cat,))


@aio
@_tx
def delete_out_of_stock():
    """همه‌ی محصولات ناموجود (موجودی صفر). خروجی: تعداد."""
    return _delete_where("stock<=0")


@aio
@_tx
def delete_products(ids):
    """حذف چند محصول انتخابی با لیست id. خروجی: تعداد."""
    ids = [int(i) for i in ids]
    total = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        total += _delete_where(f"id IN ({','.join('?' * len(chunk))})", chunk)
    return total


@aio
@_tx
def bulk_price_change(percent, category=None, step=1):
    """قیمت همه‌ی محصولات (یا یک دسته) را به‌درصد زیاد/کم می‌کند. percent منفی = کاهش.
    step: گرد کردن به نزدیک‌ترین مضرب (مثلاً 1000). خروجی: تعداد محصولِ تغییرکرده."""
    step = max(1, int(step))
    rows = _all("SELECT id,price FROM products" + (" WHERE category=?" if category else ""), (category,) if category else ())
    n = 0
    for r in rows:
        new = int(round(r["price"] * (100 + percent) / 100 / step)) * step
        new = max(0, new)
        if new != r["price"]:
            _ex("UPDATE products SET price=? WHERE id=?", (new, r["id"]))
            n += 1
    return n


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
def list_low_stock(lim=30):
    th = _int_setting("low_stock", 5)
    return _all("SELECT id,code,name,stock FROM products WHERE stock<=? ORDER BY stock, name LIMIT ?", (th, lim))


def _like(w):
    return "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@aio
def search(q, lim=20):
    exact = _all("SELECT * FROM products WHERE code=?", (ncode(q),))
    if exact:
        return exact
    words = norm(q).split()
    if not words:
        return []
    where = " AND ".join(["search LIKE ? ESCAPE '\\'"] * len(words))
    return _all(f"SELECT * FROM products WHERE {where} ORDER BY (stock>0) DESC, name LIMIT ?", [_like(w) for w in words] + [lim])


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


# ---------- coupons ----------
COUPON_REASONS = {
    "unknown": "❌ این کد وجود ندارد.",
    "inactive": "⏸ این کد فعلاً غیرفعال است.",
    "expired": "⌛ مهلت استفاده از این کد تمام شده.",
    "exhausted": "🚫 ظرفیت استفاده از این کد تمام شده.",
    "used_before": "🙅 شما قبلاً از این کد استفاده کرده‌اید.",
}


def _coupon_problem(cp, uid, sub):
    """None یعنی کوپن قابل استفاده است؛ وگرنه کلید دلیل."""
    if not cp:
        return "unknown"
    if not cp["active"]:
        return "inactive"
    if cp.get("expires_at") and today() > cp["expires_at"]:
        return "expired"
    if cp["max_uses"] and cp["used"] >= cp["max_uses"]:
        return "exhausted"
    if cp.get("one_per_user") and uid is not None and _one("SELECT 1 x FROM coupon_uses WHERE code=? AND user_id=?", (cp["code"], uid)):
        return "used_before"
    if sub is not None and (cp.get("min_amount") or 0) > sub:
        return "min_amount"
    return None


def _reason_text(key, cp=None):
    if key == "min_amount" and cp:
        return f"🛒 حداقل مبلغ خرید برای این کد {money(cp['min_amount'])} تومان است."
    return COUPON_REASONS.get(key, "❌ کد نامعتبر است.")


def coupon_label(cp):
    """متن کوتاه تخفیف کوپن: «۱۰٪» یا «۵۰٬۰۰۰ تومان» یا ترکیبی."""
    parts = []
    if cp.get("percent"):
        parts.append(f"{cp['percent']}٪")
    if cp.get("fixed"):
        parts.append(f"{money(cp['fixed'])} تومان")
    return " + ".join(parts) or "بدون تخفیف"


def _totals(items, coupon_code, uid=None):
    sub = sum(i["price"] * i["qty"] for i in items)
    raw = _one("SELECT * FROM coupons WHERE code=?", (coupon_code,)) if coupon_code else None
    problem = _coupon_problem(raw, uid, sub) if coupon_code else None
    cp = raw if raw and problem is None else None
    cdisc = 0
    if cp:
        cdisc = min(sub, sub * (cp["percent"] or 0) // 100 + (cp.get("fixed") or 0))
    tier = _one("SELECT * FROM tiers WHERE min_amount<=? ORDER BY percent DESC LIMIT 1", (sub,))
    tpct = tier["percent"] if tier else 0
    tdisc = sub * tpct // 100
    disc = max(cdisc, tdisc)
    kind, note = None, ""
    if disc > 0:
        if cdisc >= tdisc:
            kind, note = "coupon", f"کوپن {cp['code']} ({coupon_label(cp)})"
        else:
            kind, note = "tier", f"تخفیف پله‌ای ({tpct}٪)"
    ship = _int_setting("shipping_fee", 0) if sub > 0 else 0
    nxt = _one("SELECT * FROM tiers WHERE min_amount>? ORDER BY min_amount ASC LIMIT 1", (sub,))
    return {"items": items, "subtotal": sub, "coupon": cp["code"] if cp else None,
            "coupon_warn": _reason_text(problem, raw) if (coupon_code and problem) else None,
            "pct": round(disc * 100 / sub) if sub else 0, "kind": kind,
            "note": note, "discount": disc, "shipping": ship, "total": max(0, sub - disc + ship), "next_tier": nxt}


@aio
def cart_summary(uid):
    meta = _one("SELECT coupon FROM cart_meta WHERE user_id=?", (uid,))
    return _totals(_cart_items(uid), meta["coupon"] if meta else None, uid)


@aio
def apply_coupon_ex(uid, code):
    """نسخه‌ی کامل: {'ok': bool, 'coupon': dict|None, 'reason': key|None, 'text': پیام فارسی}"""
    cp = _one("SELECT * FROM coupons WHERE code=?", (ncode(code),))
    sub = sum(i["price"] * i["qty"] for i in _cart_items(uid))
    problem = _coupon_problem(cp, uid, sub)
    # اگر فقط به حداقل خرید نرسیده، کد را نگه می‌داریم تا با افزودن کالا فعال شود
    if problem and problem != "min_amount":
        return {"ok": False, "coupon": cp, "reason": problem, "text": _reason_text(problem, cp)}
    _ex("INSERT INTO cart_meta(user_id,coupon) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET coupon=excluded.coupon", (uid, cp["code"]))
    if problem == "min_amount":
        return {"ok": True, "coupon": cp, "reason": problem, "text": _reason_text(problem, cp) + " با افزودن کالا فعال می‌شود."}
    return {"ok": True, "coupon": cp, "reason": None, "text": f"✅ کوپن {coupon_label(cp)} اعمال شد."}


@aio
def apply_coupon(uid, code):
    """سازگار با نسخه‌ی قبلی: کوپن یا None."""
    r = apply_coupon_ex.__wrapped__(uid, code)
    return r["coupon"] if r["ok"] else None


@aio
def remove_coupon(uid):
    _ex("DELETE FROM cart_meta WHERE user_id=?", (uid,))


# ---------- orders ----------
@aio
@_tx
def create_order(uid, username, name, lat, lon):
    """ثبت سفارش به‌صورت اتمیک: بررسی موجودی، کم کردن موجودی، مصرف کوپن و خالی کردن سبد."""
    items = _cart_items(uid)
    if not items:
        return {"status": "empty"}
    short = [i for i in items if i["stock"] < i["qty"]]
    if short:
        return {"status": "stock", "items": short}
    meta = _one("SELECT coupon FROM cart_meta WHERE user_id=?", (uid,))
    t = _totals(items, meta["coupon"] if meta else None, uid)
    snap = [{"pid": i["pid"], "code": i["code"], "name": i["name"], "qty": i["qty"], "price": i["price"]} for i in items]
    used_coupon = t["coupon"] if t["kind"] == "coupon" else ""
    cur = _ex("INSERT INTO orders(user_id,username,name,items,subtotal,discount,discount_note,shipping,total,coupon,lat,lon,status,created_at) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (uid, username, name, json.dumps(snap, ensure_ascii=False), t["subtotal"], t["discount"], t["note"],
               t["shipping"], t["total"], used_coupon, lat, lon, STATUSES[0], now()))
    oid = cur.lastrowid
    for i in items:
        _ex("UPDATE products SET stock=stock-? WHERE id=?", (i["qty"], i["pid"]))
    if used_coupon:
        _ex("UPDATE coupons SET used=used+1 WHERE code=?", (used_coupon,))
        _ex("INSERT OR IGNORE INTO coupon_uses(code,user_id,order_id) VALUES(?,?,?)", (used_coupon, uid, oid))
    _ex("DELETE FROM cart WHERE user_id=?", (uid,))
    _ex("DELETE FROM cart_meta WHERE user_id=?", (uid,))
    low_th = _int_setting("low_stock", 5)
    ids = [i["pid"] for i in items]
    low = _all(f"SELECT id,code,name,stock FROM products WHERE stock<=? AND id IN ({','.join('?' * len(ids))})", [low_th] + ids)
    order = _one("SELECT * FROM orders WHERE id=?", (oid,))
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


def _set_status(o, status):
    _ex("UPDATE orders SET status=? WHERE id=?", (status, o["id"]))
    if status == CANCELLED:
        for it in json.loads(o["items"]):
            _ex("UPDATE products SET stock=stock+? WHERE id=?", (it["qty"], it["pid"]))
        if o["coupon"]:
            _ex("UPDATE coupons SET used=MAX(0,used-1) WHERE code=?", (o["coupon"],))
            _ex("DELETE FROM coupon_uses WHERE code=? AND order_id=?", (o["coupon"], o["id"]))


@aio
def set_order_status(oid, status):
    o = _one("SELECT * FROM orders WHERE id=?", (oid,))
    if not o:
        return None
    if o["status"] == CANCELLED or o["status"] == status:
        return dict(o, changed=False)
    _ex("BEGIN IMMEDIATE")
    try:
        _set_status(o, status)
        _ex("COMMIT")
    except Exception:
        _ex("ROLLBACK")
        raise
    o["status"] = status
    o["changed"] = True
    return o


# ---------- coupons admin ----------
@aio
def add_coupon(code, percent=0, max_uses=0, fixed=0, min_amount=0, expires_at=None, one_per_user=0):
    """ساخت یا ویرایش کوپن (تعداد استفاده‌شده حفظ می‌شود).
    percent: درصد | fixed: مبلغ ثابت تومان | expires_at: 'YYYY-MM-DD' یا None."""
    _ex("INSERT INTO coupons(code,percent,max_uses,used,active,fixed,min_amount,expires_at,one_per_user,created_at) "
        "VALUES(?,?,?,0,1,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET percent=excluded.percent, max_uses=excluded.max_uses, "
        "fixed=excluded.fixed, min_amount=excluded.min_amount, expires_at=excluded.expires_at, "
        "one_per_user=excluded.one_per_user, active=1",
        (ncode(code), percent or 0, max_uses or 0, fixed or 0, min_amount or 0, expires_at, 1 if one_per_user else 0, now()))


@aio
def get_coupon(code):
    return _one("SELECT * FROM coupons WHERE code=?", (ncode(code),))


@aio
def del_coupon(code):
    return _ex("DELETE FROM coupons WHERE code=?", (code,)).rowcount


@aio
@_tx
def del_all_coupons():
    """همه‌ی کوپن‌ها را حذف می‌کند. سبدهایی که کوپن داشتند خنثی می‌شوند. خروجی: تعداد."""
    n = _one("SELECT COUNT(*) c FROM coupons")["c"]
    _ex("DELETE FROM coupons")
    _ex("DELETE FROM cart_meta")
    return n


@aio
@_tx
def del_expired_coupons():
    """کوپن‌های منقضی‌شده یا تمام‌شده (سقف استفاده پر) را حذف می‌کند. خروجی: تعداد."""
    where = "(expires_at IS NOT NULL AND expires_at!='' AND expires_at<?) OR (max_uses>0 AND used>=max_uses)"
    n = _one(f"SELECT COUNT(*) c FROM coupons WHERE {where}", (today(),))["c"]
    _ex(f"DELETE FROM coupons WHERE {where}", (today(),))
    return n


@aio
def toggle_coupon(code):
    """فعال ↔ غیرفعال. خروجی: وضعیت جدید (True/False) یا None اگر نبود."""
    cp = _one("SELECT active FROM coupons WHERE code=?", (code,))
    if not cp:
        return None
    new = 0 if cp["active"] else 1
    _ex("UPDATE coupons SET active=? WHERE code=?", (new, code))
    return bool(new)


@aio
def list_coupons():
    rows = _all("SELECT * FROM coupons ORDER BY active DESC, code")
    t = today()
    for r in rows:
        r["expired"] = bool(r.get("expires_at") and t > r["expires_at"])
        r["full"] = bool(r["max_uses"] and r["used"] >= r["max_uses"])
    return rows


# ---------- tiers ----------
@aio
def add_tier(min_amount, percent):
    _ex("INSERT INTO tiers(min_amount,percent) VALUES(?,?) ON CONFLICT(min_amount) DO UPDATE SET percent=excluded.percent", (min_amount, percent))


@aio
def del_tier(min_amount):
    _ex("DELETE FROM tiers WHERE min_amount=?", (min_amount,))


@aio
def del_all_tiers():
    return _ex("DELETE FROM tiers").rowcount


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


# ---------- users / stats / backup ----------
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
    t = today()
    low_th = _int_setting("low_stock", 5)
    return {
        "products": _one("SELECT COUNT(*) c FROM products")["c"],
        "users": _one("SELECT COUNT(*) c FROM users WHERE active=1")["c"],
        "orders": _one("SELECT COUNT(*) c FROM orders")["c"],
        "pending": _one("SELECT COUNT(*) c FROM orders WHERE status=?", (STATUSES[0],))["c"],
        "revenue": _one("SELECT COALESCE(SUM(total),0) s FROM orders WHERE status!=?", (CANCELLED,))["s"],
        "out": _one("SELECT COUNT(*) c FROM products WHERE stock<=0")["c"],
        "low": _one("SELECT COUNT(*) c FROM products WHERE stock>0 AND stock<=?", (low_th,))["c"],
        "today_orders": _one("SELECT COUNT(*) c FROM orders WHERE created_at LIKE ? AND status!=?", (t + "%", CANCELLED))["c"],
        "today_revenue": _one("SELECT COALESCE(SUM(total),0) s FROM orders WHERE created_at LIKE ? AND status!=?", (t + "%", CANCELLED))["s"],
        "coupons": _one("SELECT COUNT(*) c FROM coupons")["c"],
        "admins": len(_ADMINS),
    }


@aio
def backup_bytes():
    """یک نسخه‌ی پشتیبان کامل و سازگار از دیتابیس (فایل .db) برمی‌گرداند؛ برای ارسال به ادمین."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dest = sqlite3.connect(path)
        try:
            _conn.backup(dest)
        finally:
            dest.close()
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
