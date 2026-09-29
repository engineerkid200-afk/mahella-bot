import json, logging, os, sqlite3, threading
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "7048088727"))
ADMIN_LINK = "https://t.me/mahdi_reshad_d"
DB_FILE = "shop.db"
PAGE_SIZE = 6
TAGLINE = "با ماهلا، پوستت ماهه، راهِ زیبایی کوتاه!"
BTN_CATS, BTN_CODE, BTN_CART, BTN_ORDERS, BTN_CONTACT = "📦 دسته‌بندی‌ها", "🔍 استعلام کد", "🛒 سبد خرید", "🧾 سفارش‌ها", "📞 ارتباط با ادمین"
STATUSES = ["در انتظار بررسی", "در حال آماده‌سازی", "ارسال شده", "تحویل داده شده", "لغو شده"]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("shop")

_MAP = {}
for i, ch in enumerate("۰۱۲۳۴۵۶۷۸۹"): _MAP[ord(ch)] = str(i)
for i, ch in enumerate("٠١٢٣٤٥٦٧٨٩"): _MAP[ord(ch)] = str(i)
_MAP.update({ord("ي"): "ی", ord("ك"): "ک", 0x200C: " "})

def norm(t): return " ".join(str(t).translate(_MAP).lower().split())
def ncode(t): return "".join(str(t).translate(_MAP).split()).upper()
def esc(v): return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def money(n):
    try: return f"{int(n):,}"
    except: return str(n)
def now(): return datetime.now().strftime("%Y-%m-%d %H:%M")
def is_owner(u): return u.effective_user and u.effective_user.id == OWNER_ID

conn = sqlite3.connect(DB_FILE, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")
lock = threading.Lock()

def init_db():
    with lock:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS products(code TEXT PRIMARY KEY, name TEXT, brand TEXT, category TEXT, desc TEXT, price INTEGER, stock INTEGER DEFAULT 0, photo_id TEXT);
        CREATE TABLE IF NOT EXISTS cart(user_id INTEGER, code TEXT, qty INTEGER, PRIMARY KEY(user_id,code));
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, items TEXT, subtotal INTEGER, discount INTEGER, shipping INTEGER, total INTEGER, coupon TEXT, lat REAL, lon REAL, status TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS coupons(code TEXT PRIMARY KEY, percent INTEGER, max_uses INTEGER DEFAULT 0, used INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, user_id INTEGER, rating INTEGER, created_at TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, first_seen TEXT);
        """)
        conn.commit()

def get_set(k, d=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return r["value"] if r else d

def set_set(k, v):
    with lock:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
        conn.commit()

def add_product(c, n, b, cat, d, p, s, ph=None):
    with lock:
        conn.execute("INSERT INTO products(code,name,brand,category,desc,price,stock,photo_id) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name,brand=excluded.brand,category=excluded.category,desc=excluded.desc,price=excluded.price,stock=excluded.stock,photo_id=COALESCE(excluded.photo_id,products.photo_id)", (c,n,b,cat,d,p,s,ph))
        conn.commit()

def get_product(c): return conn.execute("SELECT * FROM products WHERE code=?", (c,)).fetchone()
def list_cats():
    return [(r["category"], r["c"]) for r in conn.execute("SELECT category, COUNT(*) c FROM products GROUP BY category ORDER BY category").fetchall()]
def list_products(cat): return conn.execute("SELECT * FROM products WHERE category=? ORDER BY name", (cat,)).fetchall()
def search(q, lim=10):
    lq = f"%{norm(q)}%"
    return conn.execute("SELECT * FROM products WHERE lower(name) LIKE ? OR lower(brand) LIKE ? OR lower(category) LIKE ? OR code LIKE ? LIMIT ?", (lq,lq,lq,f"%{ncode(q)}%",lim)).fetchall()
def count_products(): return conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]

def cart_add(u, c, q):
    with lock:
        conn.execute("INSERT INTO cart(user_id,code,qty) VALUES(?,?,?) ON CONFLICT(user_id,code) DO UPDATE SET qty=qty+excluded.qty", (u,c,q))
        conn.commit()
def cart_set(u, c, q):
    with lock:
        if q <= 0: conn.execute("DELETE FROM cart WHERE user_id=? AND code=?", (u,c))
        else: conn.execute("INSERT INTO cart(user_id,code,qty) VALUES(?,?,?) ON CONFLICT(user_id,code) DO UPDATE SET qty=excluded.qty", (u,c,q))
        conn.commit()
def cart_clear(u):
    with lock:
        conn.execute("DELETE FROM cart WHERE user_id=?", (u,))
        conn.commit()
def cart_items(u):
    return conn.execute("SELECT c.code, c.qty, p.name, p.price, p.stock FROM cart c JOIN products p ON p.code=c.code WHERE c.user_id=?", (u,)).fetchall()

def create_order(uid, un, items, sub, disc, ship, tot, cp, lat, lon):
    with lock:
        cur = conn.execute("INSERT INTO orders(user_id,username,items,subtotal,discount,shipping,total,coupon,lat,lon,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (uid,un,json.dumps(items,ensure_ascii=False),sub,disc,ship,tot,cp,lat,lon,STATUSES[0],now()))
        oid = cur.lastrowid
        for it in items: conn.execute("UPDATE products SET stock=MAX(0,stock-?) WHERE code=?", (it["qty"], it["code"]))
        conn.commit()
        return oid

def user_orders(uid, lim=15): return conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid,lim)).fetchall()
def recent_orders(lim=15): return conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (lim,)).fetchall()
def set_status(oid, s):
    with lock:
        conn.execute("UPDATE orders SET status=? WHERE id=?", (s, oid))
        conn.commit()
def add_coupon(c, p, m=0):
    with lock:
        conn.execute("INSERT INTO coupons(code,percent,max_uses,used,active) VALUES(?,?,?,0,1) ON CONFLICT(code) DO UPDATE SET percent=excluded.percent,max_uses=excluded.max_uses,active=1", (c,p,m))
        conn.commit()
def get_coupon(c): return conn.execute("SELECT * FROM coupons WHERE code=? AND active=1", (c,)).fetchone()
def use_coupon(c):
    with lock:
        conn.execute("UPDATE coupons SET used=used+1 WHERE code=?", (c,))
        conn.commit()
def add_review(c, u, r):
    with lock:
        conn.execute("INSERT INTO reviews(code,user_id,rating,created_at) VALUES(?,?,?,?)", (c,u,r,now()))
        conn.commit()
def rating(c):
    r = conn.execute("SELECT AVG(rating) a, COUNT(*) c FROM reviews WHERE code=?", (c,)).fetchone()
    return (round(r["a"], 1) if r["a"] else 0, r["c"])
def track_user(u):
    with lock:
        conn.execute("INSERT OR IGNORE INTO users(user_id, first_seen) VALUES(?,?)", (u, now()))
        conn.commit()
def all_users(): return [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
def ship_fee():
    try: return int(get_set("shipping_fee", "0"))
    except: return 0

init_db()
if get_set("shipping_fee") is None: set_set("shipping_fee", "0")

MAIN_KB = ReplyKeyboardMarkup([[BTN_CATS, BTN_CART], [BTN_CODE, BTN_ORDERS], [BTN_CONTACT]], resize_keyboard=True)
LOC_KB = ReplyKeyboardMarkup([[KeyboardButton("📍 ارسال موقعیت", request_location=True)], ["❌ انصراف"]], resize_keyboard=True)

def welcome():
    return f"🌸 <b>{TAGLINE}</b>\n\n👋 خوش آمدید!\n📦 تعداد محصولات: <b>{count_products()}</b>\n\nکد یا نام محصول را بفرستید یا از منو استفاده کنید."

def caption(c):
    p = get_product(c)
    if not p: return "محصول یافت نشد."
    avg, cnt = rating(c)
    rat = f"⭐ {avg}/5 ({cnt} نظر)" if cnt else "⭐ بدون امتیاز"
    st = "❌ ناموجود" if p["stock"] <= 0 else f"📦 موجودی: {p['stock']}"
    return f"🏷 کد: <code>{esc(c)}</code>\n🛍 {esc(p['name'])}\n🏷 {esc(p['brand'])}\n📂 {esc(p['category'])}\n{st}\n{rat}\n\n📝 {esc(p['desc'])}\n\n💰 <b>{money(p['price'])} تومان</b>"

def product_kb(c):
    p = get_product(c)
    btns = []
    if p and p["stock"] > 0: btns.append([InlineKeyboardButton("🛒 افزودن به سبد", callback_data=f"add:{c}")])
    btns.append([InlineKeyboardButton("⭐ نظرات", callback_data=f"rev:{c}")])
    btns.append([InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="cats")])
    return InlineKeyboardMarkup(btns)

def cats_kb():
    cats = list_cats()
    if not cats: return InlineKeyboardMarkup([[InlineKeyboardButton("❌ محصولی ثبت نشده", callback_data="noop")]])
    rows, row = [], []
    for i, (cat, c) in enumerate(cats):
        row.append(InlineKeyboardButton(f"{cat} ({c})", callback_data=f"cat:{i}:0"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def cat_page(idx, page):
    cats = list_cats()
    if idx >= len(cats): return "دسته یافت نشد.", cats_kb()
    cat = cats[idx][0]
    prods = list_products(cat)
    tp = max(1, -(-len(prods) // PAGE_SIZE))
    page = max(0, min(page, tp - 1))
    chunk = prods[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
    lines = [f"📦 <b>{esc(cat)}</b> — {len(prods)} محصول (صفحه {page+1}/{tp})\n"]
    btns = []
    for p in chunk:
        m = "❌" if p["stock"] <= 0 else "✅"
        lines.append(f"{m} {esc(p['name'])} — {money(p['price'])} تومان (کد {esc(p['code'])})")
        btns.append([InlineKeyboardButton(f"🔎 {p['name'][:38]}", callback_data=f"p:{p['code']}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"cat:{idx}:{page-1}"))
    if page < tp - 1: nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"cat:{idx}:{page+1}"))
    if nav: btns.append(nav)
    btns.append([InlineKeyboardButton("🔙 دسته‌ها", callback_data="cats")])
    return "\n".join(lines), InlineKeyboardMarkup(btns)

def cart_view(uid, cp_code=None):
    items = cart_items(uid)
    if not items: return "🛒 سبد خالی است.", None
    lines = ["🛒 <b>سبد خرید شما</b>\n"]
    btns = []
    sub = 0
    for it in items:
        lt = it["price"] * it["qty"]
        sub += lt
        lines.append(f"• {esc(it['name'])} × {it['qty']} = {money(lt)} تومان")
        btns.append([
            InlineKeyboardButton("➖", callback_data=f"cc:dec:{it['code']}"),
            InlineKeyboardButton(f"{it['qty']}", callback_data="noop"),
            InlineKeyboardButton("➕", callback_data=f"cc:inc:{it['code']}"),
            InlineKeyboardButton("🗑", callback_data=f"cc:del:{it['code']}"),
        ])
    cd = 0; ac = None
    if cp_code:
        cp = get_coupon(cp_code)
        if cp: cd = sub * cp["percent"] // 100; ac = cp["code"]
    ship = ship_fee()
    tot = max(0, sub - cd + ship)
    lines.append(f"\n💵 جمع: {money(sub)} تومان")
    if ac: lines.append(f"🎫 کوپن: -{money(cd)} تومان")
    if ship: lines.append(f"🚚 ارسال: {money(ship)} تومان")
    lines.append(f"✅ <b>پرداخت: {money(tot)} تومان</b>")
    btns.append([InlineKeyboardButton("🎟 کوپن", callback_data="enter_coupon")])
    btns.append([InlineKeyboardButton("🧾 نهایی‌سازی سفارش", callback_data="checkout")])
    btns.append([InlineKeyboardButton("🗑 خالی کردن سبد", callback_data="cart_clear")])
    return "\n".join(lines), InlineKeyboardMarkup(btns)

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    track_user(u.effective_user.id)
    await u.message.reply_text(welcome(), parse_mode=ParseMode.HTML, reply_markup=MAIN_KB)

async def cmd_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    t = "ℹ️ <b>راهنما</b>\n\n• برای محصولات روی «دسته‌بندی‌ها» بزنید.\n• کد یا نام محصول را ارسال کنید.\n• سفارش با ارسال موقعیت مکانی ثبت می‌شود."
    if is_owner(u): t += "\n\n👑 ادمین: /admin"
    await u.message.reply_text(t, parse_mode=ParseMode.HTML)

async def cmd_orders(u: Update, c: ContextTypes.DEFAULT_TYPE):
    os_ = user_orders(u.effective_user.id)
    if not os_:
        await u.message.reply_text("🧾 سفارشی ثبت نکرده‌اید."); return
    lines = ["🧾 <b>سفارش‌ها</b>\n"]
    for o in os_: lines.append(f"#{o['id']} — {money(o['total'])} تومان — <b>{esc(o['status'])}</b>\n<i>{esc(o['created_at'])}</i>\n")
    await u.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def on_msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    txt = u.message.text.strip()
    uid = u.effective_user.id

    if c.user_data.get("awaiting_coupon"):
        cp = get_coupon(ncode(txt))
        if not cp: await u.message.reply_text("❌ کوپن نامعتبر.")
        else:
            c.user_data["applied_coupon"] = cp["code"]
            await u.message.reply_text(f"✅ کوپن {cp['percent']}% اعمال شد.")
        c.user_data["awaiting_coupon"] = False
        body, kb = cart_view(uid, c.user_data.get("applied_coupon"))
        await u.message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if c.user_data.get("awaiting_review"):
        try:
            r = int(ncode(txt))
            if r < 1 or r > 5: raise ValueError
        except:
            await u.message.reply_text("عدد ۱ تا ۵ بفرستید."); return
        add_review(c.user_data.get("review_code"), uid, r)
        c.user_data["awaiting_review"] = False
        await u.message.reply_text("✅ نظر ثبت شد.")
        return

    if txt == BTN_CATS: await u.message.reply_text("📂 دسته‌بندی‌ها:", reply_markup=cats_kb()); return
    if txt == BTN_CART:
        body, kb = cart_view(uid, c.user_data.get("applied_coupon"))
        await u.message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=kb); return
    if txt == BTN_ORDERS: await cmd_orders(u, c); return
    if txt == BTN_CONTACT:
        await u.message.reply_text("📞 ارتباط با ادمین:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 پیام به ادمین", url=ADMIN_LINK)]])); return
    if txt == BTN_CODE: await u.message.reply_text("🔍 کد محصول را بفرستید:"); return
    if txt == "❌ انصراف":
        c.user_data.clear(); await u.message.reply_text("❌ لغو شد.", reply_markup=MAIN_KB); return

    results = search(txt)
    if results:
        for p in results[:5]:
            try:
                if p["photo_id"]:
                    await u.message.reply_photo(photo=p["photo_id"], caption=caption(p["code"]), parse_mode=ParseMode.HTML, reply_markup=product_kb(p["code"]))
                else:
                    await u.message.reply_text(caption(p["code"]), parse_mode=ParseMode.HTML, reply_markup=product_kb(p["code"]))
            except Exception as e: log.error(e)
        return

    await u.message.reply_text("❌ محصولی یافت نشد.")

async def on_loc(u: Update, c: ContextTypes.DEFAULT_TYPE):
    user = u.effective_user
    loc = u.message.location
    items = c.user_data.get("checkout_items") or cart_items(user.id)
    if not items:
        await u.message.reply_text("سبد خالی است.", reply_markup=MAIN_KB); return
    sub = sum(it["price"] * it["qty"] for it in items)
    cd = 0; cp_code = c.user_data.get("applied_coupon")
    if cp_code:
        cp = get_coupon(cp_code)
        if cp: cd = sub * cp["percent"] // 100; use_coupon(cp_code)
    ship = ship_fee()
    tot = max(0, sub - cd + ship)
    oid = create_order(user.id, user.username or user.full_name, [{"code": it["code"], "name": it["name"], "qty": it["qty"], "price": it["price"]} for it in items], sub, cd, ship, tot, cp_code or "", loc.latitude, loc.longitude)
    cart_clear(user.id)
    c.user_data.clear()
    await u.message.reply_text(f"✅ <b>سفارش #{oid} ثبت شد!</b>\n💵 {money(tot)} تومان\n\nبه زودی تماس می‌گیریم. 🙏", parse_mode=ParseMode.HTML, reply_markup=MAIN_KB)
    try:
        await c.bot.send_message(OWNER_ID, f"🔔 سفارش #{oid}\n👤 {esc(user.full_name)} (@{esc(user.username or '-')})\n💵 {money(tot)} تومان")
        await c.bot.send_location(OWNER_ID, latitude=loc.latitude, longitude=loc.longitude)
    except Exception as e: log.error(e)

async def on_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    d = q.data
    await q.answer()
    if d == "noop": return
    if d == "cats": await q.message.reply_text("📂 دسته‌ها:", reply_markup=cats_kb()); return
    if d.startswith("cat:"):
        _, i, p = d.split(":")
        body, kb = cat_page(int(i), int(p))
        try: await q.edit_message_text(body, parse_mode=ParseMode.HTML, reply_markup=kb)
        except: await q.message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    if d.startswith("p:"):
        code = d.split(":", 1)[1]; p = get_product(code)
        if not p: await q.message.reply_text("یافت نشد."); return
        if p["photo_id"]: await q.message.reply_photo(photo=p["photo_id"], caption=caption(code), parse_mode=ParseMode.HTML, reply_markup=product_kb(code))
        else: await q.message.reply_text(caption(code), parse_mode=ParseMode.HTML, reply_markup=product_kb(code))
        return
    if d.startswith("add:"):
        code = d.split(":", 1)[1]; p = get_product(code)
        if not p or p["stock"] <= 0: await q.answer("❌ ناموجود", show_alert=True); return
        cart_add(q.from_user.id, code, 1)
        await q.answer(f"✅ {p['name']} اضافه شد")
        return
    if d.startswith("cc:"):
        _, act, code = d.split(":")
        items = {it["code"]: it for it in cart_items(q.from_user.id)}
        if code not in items: await q.answer("یافت نشد", show_alert=True); return
        cur = items[code]["qty"]
        if act == "inc": cart_set(q.from_user.id, code, cur + 1)
        elif act == "dec": cart_set(q.from_user.id, code, cur - 1)
        elif act == "del": cart_set(q.from_user.id, code, 0)
        body, kb = cart_view(q.from_user.id, c.user_data.get("applied_coupon"))
        try: await q.edit_message_text(body, parse_mode=ParseMode.HTML, reply_markup=kb)
        except: pass
        return
    if d == "cart_clear": cart_clear(q.from_user.id); await q.message.reply_text("🗑 سبد خالی شد."); return
    if d == "enter_coupon": c.user_data["awaiting_coupon"] = True; await q.message.reply_text("🎟 کد کوپن:"); return
    if d == "checkout":
        items = cart_items(q.from_user.id)
        if not items: await q.message.reply_text("سبد خالی است."); return
        c.user_data["checkout_items"] = [dict(i) for i in items]
        await q.message.reply_text("📍 موقعیت مکانی خود را بفرستید:", reply_markup=LOC_KB); return
    if d.startswith("rev:"):
        code = d.split(":", 1)[1]
        avg, cnt = rating(code)
        await q.message.reply_text(f"⭐ امتیاز: {avg}/5 ({cnt} نظر)\n\nبرای ثبت نظر روی دکمه بزنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✍️ ثبت نظر", callback_data=f"wrev:{code}")]]))
        return
    if d.startswith("wrev:"):
        c.user_data["review_code"] = d.split(":", 1)[1]
        c.user_data["awaiting_review"] = True
        await q.message.reply_text("امتیاز ۱ تا ۵:"); return

async def cmd_admin(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u): await u.message.reply_text("⛔"); return
    t = ("👑 <b>پنل ادمین</b>\n\n"
         "/addproduct — افزودن:\n<code>/addproduct کد|نام|برند|دسته|توضیح|قیمت|موجودی</code>\n\n"
         "/delproduct کد\n/allorders\n/setstatus شماره|وضعیت\n/addcoupon کد|درصد\n/setship مبلغ\n/broadcast متن\n/stats")
    await u.message.reply_text(t, parse_mode=ParseMode.HTML)

async def a_add(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u): return
    try:
        parts = [p.strip() for p in u.message.text.split(maxsplit=1)[1].split("|")]
        add_product(parts[0], parts[1], parts[2], parts[3], parts[4], int(parts[5]), int(parts[6]))
        await u.message.reply_text(f"✅ محصول {parts[0]} ذخیره شد.")
    except:
        await u.message.reply_text("❌ فرمت: /addproduct کد|نام|برند|دسته|توضیح|قیمت|موجودی")

async def a_del(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u): return
    code = u.message.text.split(maxsplit=1)[1].strip()
    with lock: conn.execute("DELETE FROM products WHERE code=?", (code,)); conn.commit()
    await u.message.reply_text("🗑 حذف شد.")

async def a_orders(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u): return
    os_ = recent_orders()
    if not os_: await u.message.reply_text("سفارشی نیست."); return
    lines = ["📋 <b>سفارش‌ها</b>\n"]
    for o in os_: lines.append(f"#{o['id']} — {money(o['total'])} — {o['status']} — {o['created_at']}")
    await u.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def a_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u): return
    try:
        oid, st = u.message.text.split(maxsplit=1)[1].split("|", 1)
        set_status(int(oid.strip()), st.strip())
        await u.message.reply_text("✅ وضعیت تغییر کرد.")
    except: await u.message.reply_text("❌ فرمت: /setstatus 123|ارسال شده")

async def a_coupon(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u): return
    try:
        parts = u.message.text.split(maxsplit=1)[1].split("|")
        add_coupon(ncode(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
        await u.message.reply_text("✅ کوپن ثبت شد.")
    except: await u.message.reply_text("❌ فرمت: /addcoupon کد|درصد|حداکثر")

async def a_setship(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u): return
    try:
        v = int(u.message.text.split(maxsplit=1)[1].strip())
        set_set("shipping_fee", str(v))
        await u.message.reply_text(f"✅ هزینه ارسال: {money(v)}")
    except: await u.message.reply_text("❌ /setship مبلغ")

async def a_broadcast(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u): return
    msg = u.message.text.split(maxsplit=1)[1]
    sent = 0
    for uid in all_users():
        try: await c.bot.send_message(uid, msg); sent += 1
        except: pass
    await u.message.reply_text(f"✅ ارسال به {sent} نفر.")

async def a_stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_owner(u): return
    p = count_products()
    o = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    usr = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    await u.message.reply_text(f"📊 محصولات: {p}\n🧾 سفارش‌ها: {o}\n👥 کاربران: {usr}")

def main():
    print("🤖 ربات روشن می‌شود...")
    if not BOT_TOKEN:
        print("❌ متغیر BOT_TOKEN تنظیم نشده!")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("addproduct", a_add))
    app.add_handler(CommandHandler("delproduct", a_del))
    app.add_handler(CommandHandler("allorders", a_orders))
    app.add_handler(CommandHandler("setstatus", a_status))
    app.add_handler(CommandHandler("addcoupon", a_coupon))
    app.add_handler(CommandHandler("setship", a_setship))
    app.add_handler(CommandHandler("broadcast", a_broadcast))
    app.add_handler(CommandHandler("stats", a_stats))
    app.add_handler(MessageHandler(filters.LOCATION, on_loc))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
    print("✅ ربات آماده!")
    app.run_polling()

if __name__ == "__main__":
    main()
