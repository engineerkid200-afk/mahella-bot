import asyncio
import hashlib
import json
import logging
import os
import re

from telegram import (InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, KeyboardButton,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import db
from excel_import import export_xlsx, parse_file
from util import esc, money, ncode, to_int

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "7048088727"))
ADMIN_LINK = os.getenv("ADMIN_LINK", "https://t.me/mahdi_reshad_d")
TAGLINE = "با ماهلا، پوستت ماهه، راهِ زیبایی کوتاه!"
PAGE_SIZE = 6
HTML = ParseMode.HTML
STATUSES = db.STATUSES
STATUS_ICON = {STATUSES[0]: "⏳", STATUSES[1]: "🔄", STATUSES[2]: "🚚", STATUSES[3]: "✅", STATUSES[4]: "❌"}

BTN_CATS, BTN_CODE, BTN_CART, BTN_ORDERS, BTN_CONTACT = "📦 دسته‌بندی‌ها", "🔍 جستجو / استعلام کد", "🛒 سبد خرید", "🧾 سفارش‌ها", "📞 ارتباط با ادمین"
BTN_CANCEL = "❌ انصراف"
MENU_TEXTS = {BTN_CATS, BTN_CODE, BTN_CART, BTN_ORDERS, BTN_CONTACT, BTN_CANCEL}
MAIN_KB = ReplyKeyboardMarkup([[BTN_CATS, BTN_CART], [BTN_CODE, BTN_ORDERS], [BTN_CONTACT]], resize_keyboard=True)
LOC_KB = ReplyKeyboardMarkup([[KeyboardButton("📍 ارسال موقعیت", request_location=True)], [BTN_CANCEL]], resize_keyboard=True)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("shop")


def is_owner(u):
    return bool(u.effective_user) and u.effective_user.id == OWNER_ID


def chash(name):
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:8]


async def show(q, text, kb=None):
    """پیام را ویرایش می‌کند؛ اگر نشد پیام جدید می‌فرستد."""
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await q.message.reply_text(text, parse_mode=HTML, reply_markup=kb)


# ====================== نمایش محصول ======================
def caption(p, rate):
    avg, cnt = rate
    rat = f"⭐ {avg}/5 ({cnt} نظر)" if cnt else "⭐ بدون امتیاز"
    st = "❌ ناموجود" if p["stock"] <= 0 else f"📦 موجودی: {p['stock']}"
    lines = [f"🏷 کد: <code>{esc(p['code'])}</code>", f"🛍 <b>{esc(p['name'])}</b>"]
    if p["brand"]:
        lines.append(f"🏷 برند: {esc(p['brand'])}")
    lines += [f"📂 {esc(p['category'])}", st, rat]
    if p["description"]:
        lines.append(f"\n📝 {esc(p['description'][:350])}")
    extra = json.loads(p.get("extra") or "{}")
    if extra:
        lines.append("\n📋 " + " | ".join(f"{esc(k)}: {esc(v)}" for k, v in list(extra.items())[:6])[:300])
    lines.append(f"\n💰 <b>{money(p['price'])} تومان</b>")
    return "\n".join(lines)[:1000]


def product_kb(p, n=1, owner=False):
    pid = p["id"]
    rows = []
    if p["stock"] > 0:
        rows.append([IB("➖", callback_data=f"q:{pid}:{n - 1}"), IB(f"{n}", callback_data="noop"), IB("➕", callback_data=f"q:{pid}:{n + 1}")])
        rows.append([IB(f"🛒 افزودن {n} عدد به سبد", callback_data=f"add:{pid}:{n}")])
    rows.append([IB("⭐ امتیاز و نظرات", callback_data=f"rev:{pid}")])
    if owner:
        rows.append([IB("⚙️ مدیریت این محصول", callback_data=f"a:pm:{pid}")])
    rows.append([IB("🔙 دسته‌بندی‌ها", callback_data="cats")])
    return IM(rows)


async def send_product(msg, p, owner=False):
    cap = caption(p, await db.rating(p["id"]))
    kb = product_kb(p, 1, owner)
    if p.get("photo"):
        try:
            await msg.reply_photo(photo=p["photo"], caption=cap, parse_mode=HTML, reply_markup=kb)
            return
        except TelegramError as e:
            log.warning("عکس محصول %s ارسال نشد: %s", p["code"], e)
    await msg.reply_text(cap, parse_mode=HTML, reply_markup=kb)


async def cats_kb():
    cats = await db.categories()
    if not cats:
        return IM([[IB("❌ هنوز محصولی ثبت نشده", callback_data="noop")]])
    rows, row = [], []
    for cat, n in cats:
        row.append(IB(f"{cat} ({n})", callback_data=f"c:{chash(cat)}:0"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return IM(rows)


async def cat_page(h, page):
    cat = next((c for c, _ in await db.categories() if chash(c) == h), None)
    if cat is None:
        return "دسته پیدا نشد.", await cats_kb()
    prods = await db.products_in(cat)
    tp = max(1, -(-len(prods) // PAGE_SIZE))
    page = max(0, min(page, tp - 1))
    lines = [f"📦 <b>{esc(cat)}</b> — {len(prods)} محصول (صفحه {page + 1}/{tp})\n"]
    btns = []
    for p in prods[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        m = "❌" if p["stock"] <= 0 else "✅"
        lines.append(f"{m} {esc(p['name'])} — {money(p['price'])} تومان")
        btns.append([IB(f"🔎 {p['name'][:38]}", callback_data=f"p:{p['id']}")])
    nav = []
    if page > 0:
        nav.append(IB("⬅️ قبلی", callback_data=f"c:{h}:{page - 1}"))
    if page < tp - 1:
        nav.append(IB("بعدی ➡️", callback_data=f"c:{h}:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([IB("🔙 دسته‌ها", callback_data="cats")])
    return "\n".join(lines), IM(btns)


# ====================== سبد و سفارش ======================
async def cart_view(uid):
    s = await db.cart_summary(uid)
    if not s["items"]:
        return "🛒 سبد خرید خالی است.", None
    lines = ["🛒 <b>سبد خرید شما</b>\n"]
    rows = []
    for i, it in enumerate(s["items"], 1):
        warn = f" ⚠️ فقط {it['stock']} موجود" if it["stock"] < it["qty"] else ""
        lines.append(f"{i}. {esc(it['name'])} × {it['qty']} = {money(it['price'] * it['qty'])} تومان{warn}")
        rows.append([IB("➖", callback_data=f"cc:dec:{it['pid']}"), IB(f"{i}) ×{it['qty']}", callback_data="noop"),
                     IB("➕", callback_data=f"cc:inc:{it['pid']}"), IB("🗑", callback_data=f"cc:del:{it['pid']}")])
    lines.append(f"\n💵 جمع: {money(s['subtotal'])} تومان")
    if s["discount"]:
        lines.append(f"🎁 {esc(s['note'])}: -{money(s['discount'])} تومان")
    if s["shipping"]:
        lines.append(f"🚚 هزینه ارسال: {money(s['shipping'])} تومان")
    lines.append(f"✅ <b>مبلغ قابل پرداخت: {money(s['total'])} تومان</b>")
    nt = s["next_tier"]
    if nt:
        lines.append(f"\n💡 با {money(nt['min_amount'] - s['subtotal'])} تومان خرید بیشتر، {nt['percent']}٪ تخفیف می‌گیری.")
    rows.append([IB("🎟 حذف کوپن" if s["coupon"] else "🎟 کد تخفیف دارم", callback_data="coupon_off" if s["coupon"] else "coupon")])
    rows.append([IB("🧾 نهایی‌سازی سفارش", callback_data="checkout")])
    rows.append([IB("🗑 خالی کردن سبد", callback_data="cart_clear")])
    return "\n".join(lines), IM(rows)


def order_text(o, admin=False):
    items = json.loads(o["items"])
    lines = [f"🧾 <b>سفارش #{o['id']}</b> — {STATUS_ICON.get(o['status'], '')} {esc(o['status'])}", f"🕒 {o['created_at']}"]
    if admin:
        who = esc(o["name"] or "-") + (f" (@{esc(o['username'])})" if o["username"] else "")
        lines.append(f"👤 {who} — <a href=\"tg://user?id={o['user_id']}\">پیام به مشتری</a>")
    lines.append("")
    for it in items:
        lines.append(f"• {esc(it['name'])} × {it['qty']} = {money(it['price'] * it['qty'])}")
    lines.append(f"\n💵 جمع: {money(o['subtotal'])}")
    if o["discount"]:
        lines.append(f"🎁 {esc(o['discount_note'] or 'تخفیف')}: -{money(o['discount'])}")
    if o["shipping"]:
        lines.append(f"🚚 ارسال: {money(o['shipping'])}")
    lines.append(f"✅ <b>پرداخت: {money(o['total'])} تومان</b>")
    return "\n".join(lines)


def status_kb(o):
    btns = [IB(f"{STATUS_ICON[s]} {s}", callback_data=f"a:st:{o['id']}:{i}") for i, s in enumerate(STATUSES)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([IB("🔙 لیست سفارش‌ها", callback_data="a:orders")])
    return IM(rows)


async def cmd_orders(u, c):
    orders = await db.user_orders(u.effective_user.id)
    if not orders:
        await u.effective_message.reply_text("🧾 هنوز سفارشی ثبت نکرده‌اید.")
        return
    lines = ["🧾 <b>سفارش‌های شما</b>\n"]
    for o in orders:
        lines.append(f"{STATUS_ICON.get(o['status'], '')} #{o['id']} — {money(o['total'])} تومان — <b>{esc(o['status'])}</b>\n<i>{o['created_at']}</i>\n")
    await u.effective_message.reply_text("\n".join(lines), parse_mode=HTML)


async def on_location(u, c):
    if not c.user_data.get("await_loc"):
        await u.message.reply_text("برای ثبت سفارش، از سبد خرید روی «نهایی‌سازی سفارش» بزنید.", reply_markup=MAIN_KB)
        return
    user, loc = u.effective_user, u.message.location
    res = await db.create_order(user.id, user.username or "", user.full_name, loc.latitude, loc.longitude)
    if res["status"] == "empty":
        c.user_data.pop("await_loc", None)
        await u.message.reply_text("🛒 سبد خالی است.", reply_markup=MAIN_KB)
        return
    if res["status"] == "stock":
        names = "\n".join(f"• {esc(i['name'])} (موجودی: {i['stock']}، در سبد: {i['qty']})" for i in res["items"])
        c.user_data.pop("await_loc", None)
        await u.message.reply_text(f"⚠️ موجودی این کالاها کافی نیست:\n{names}\n\nسبد را اصلاح کنید.", parse_mode=HTML, reply_markup=MAIN_KB)
        body, kb = await cart_view(user.id)
        await u.message.reply_text(body, parse_mode=HTML, reply_markup=kb)
        return
    c.user_data.pop("await_loc", None)
    o = res["order"]
    await u.message.reply_text(f"✅ <b>سفارش #{o['id']} ثبت شد!</b>\n💵 مبلغ: {money(o['total'])} تومان\n\nوضعیت سفارش را از «🧾 سفارش‌ها» ببینید. به زودی با شما تماس می‌گیریم 🙏",
                               parse_mode=HTML, reply_markup=MAIN_KB)
    try:
        m = await c.bot.send_message(OWNER_ID, "🔔 <b>سفارش جدید</b>\n\n" + order_text(o, True), parse_mode=HTML, reply_markup=status_kb(o))
        await c.bot.send_location(OWNER_ID, latitude=loc.latitude, longitude=loc.longitude)
        for p in res["low"]:
            await c.bot.send_message(OWNER_ID, f"⚠️ موجودی کم: {esc(p['name'])} (کد {esc(p['code'])}) — {p['stock']} عدد مانده", parse_mode=HTML)
    except TelegramError as e:
        log.error("اطلاع‌رسانی به ادمین ناموفق: %s", e)


# ====================== دستورات و پیام‌ها ======================
async def cmd_start(u, c):
    await db.track_user(u.effective_user.id, u.effective_user.full_name)
    c.user_data["seen"] = True
    c.user_data.pop("flow", None)
    n = await db.count_products()
    await u.message.reply_text(f"🌸 <b>{TAGLINE}</b>\n\n👋 خوش آمدید!\n📦 تعداد محصولات: <b>{n}</b>\n\nنام یا کد محصول را بفرستید یا از منو استفاده کنید.",
                               parse_mode=HTML, reply_markup=MAIN_KB)


async def cmd_help(u, c):
    t = "ℹ️ <b>راهنما</b>\n\n• روی «دسته‌بندی‌ها» بزنید و محصول را انتخاب کنید.\n• نام یا کد محصول را بفرستید تا پیدا شود.\n• با ➕/➖ تعداد را انتخاب و به سبد اضافه کنید.\n• در سبد، سفارش را نهایی و موقعیت مکانی‌تان را بفرستید."
    if is_owner(u):
        t += "\n\n👑 ادمین: /admin"
    await u.message.reply_text(t, parse_mode=HTML)


async def cmd_cancel(u, c):
    c.user_data.pop("flow", None)
    c.user_data.pop("await_loc", None)
    c.user_data.pop("awaiting", None)
    await u.message.reply_text("❌ لغو شد.", reply_markup=MAIN_KB)


async def on_message(u, c):
    m = u.message
    if not m or not u.effective_user:
        return
    uid = u.effective_user.id
    if not c.user_data.get("seen"):
        await db.track_user(uid, u.effective_user.full_name)
        c.user_data["seen"] = True
    if m.location:
        return await on_location(u, c)
    if uid == OWNER_ID:
        flow = c.user_data.get("flow")
        if flow and (m.text or "").strip() in MENU_TEXTS:
            c.user_data.pop("flow", None)
            flow = None
        if flow:
            return await owner_flow(u, c, flow)
        if m.document:
            return await import_excel(u, c)
        if m.photo:
            return await owner_photo(u, c)
    if m.text:
        return await on_text(u, c)


async def on_text(u, c):
    txt = u.message.text.strip()
    uid = u.effective_user.id
    aw = c.user_data.get("awaiting")
    if aw and txt not in MENU_TEXTS:
        c.user_data.pop("awaiting", None)
        if aw["t"] == "coupon":
            cp = await db.apply_coupon(uid, txt)
            await u.message.reply_text(f"✅ کوپن {cp['percent']}٪ اعمال شد." if cp else "❌ کوپن نامعتبر یا تمام‌شده است.")
            body, kb = await cart_view(uid)
            await u.message.reply_text(body, parse_mode=HTML, reply_markup=kb)
        elif aw["t"] == "comment":
            if txt != "-":
                await db.add_comment(aw["pid"], uid, txt[:500])
            await u.message.reply_text("🙏 ممنون از نظرتان!")
        return
    if txt in MENU_TEXTS:
        c.user_data.pop("awaiting", None)
    if txt == BTN_CATS:
        await u.message.reply_text("📂 دسته‌بندی‌ها:", reply_markup=await cats_kb())
    elif txt == BTN_CART:
        body, kb = await cart_view(uid)
        await u.message.reply_text(body, parse_mode=HTML, reply_markup=kb)
    elif txt == BTN_ORDERS:
        await cmd_orders(u, c)
    elif txt == BTN_CONTACT:
        await u.message.reply_text("📞 ارتباط با ادمین:", reply_markup=IM([[IB("💬 پیام به ادمین", url=ADMIN_LINK)]]))
    elif txt == BTN_CODE:
        await u.message.reply_text("🔍 نام یا کد محصول را بفرستید:")
    elif txt == BTN_CANCEL:
        c.user_data.pop("await_loc", None)
        await u.message.reply_text("❌ لغو شد.", reply_markup=MAIN_KB)
    else:
        res = await db.search(txt)
        if not res:
            await u.message.reply_text("❌ محصولی پیدا نشد. نام یا کد را دقیق‌تر بفرستید یا از «دسته‌بندی‌ها» استفاده کنید.")
            return
        for p in res[:3]:
            await send_product(u.message, p, is_owner(u))
        if len(res) > 3:
            await u.message.reply_text(f"🔎 {len(res) - 3} نتیجه‌ی دیگر:",
                                       reply_markup=IM([[IB(f"{p['name'][:30]} — {money(p['price'])}", callback_data=f"p:{p['id']}")] for p in res[3:13]]))


# ====================== دکمه‌های شیشه‌ای کاربران ======================
async def on_cb(u, c):
    q = u.callback_query
    d = q.data or ""
    done = False

    async def ans(text=None, alert=False):
        nonlocal done
        if done:
            return
        done = True
        try:
            await q.answer(text, show_alert=alert)
        except TelegramError:
            pass

    if not d.startswith(("add:", "cc:", "a:")):
        await ans()
    try:
        if d.startswith("a:"):
            if q.from_user.id != OWNER_ID:
                await ans("⛔", True)
                return
            await ans()
            await admin_cb(q, c, d[2:])
        else:
            await user_cb(q, c, d, ans)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    finally:
        await ans()


async def user_cb(q, c, d, ans):
    uid = q.from_user.id
    owner = uid == OWNER_ID
    if d == "noop":
        return
    if d == "cats":
        await q.message.reply_text("📂 دسته‌بندی‌ها:", reply_markup=await cats_kb())
    elif d.startswith("c:"):
        _, h, pg = d.split(":")
        text, kb = await cat_page(h, int(pg))
        await show(q, text, kb)
    elif d.startswith("p:"):
        p = await db.get_product(int(d[2:]))
        if p:
            await send_product(q.message, p, owner)
        else:
            await q.message.reply_text("محصول پیدا نشد.")
    elif d.startswith("q:"):
        _, pid, n = d.split(":")
        p = await db.get_product(int(pid))
        if p and p["stock"] > 0:
            n = max(1, min(int(n), p["stock"]))
            await q.edit_message_reply_markup(reply_markup=product_kb(p, n, owner))
    elif d.startswith("add:"):
        _, pid, n = d.split(":")
        r = await db.cart_add(uid, int(pid), max(1, int(n)))
        if r is None:
            await ans("❌ ناموجود", True)
        else:
            msg = f"✅ {r['added']} عدد اضافه شد (در سبد: {r['qty']})"
            if r["capped"]:
                msg += f"\nبیشتر از موجودی نمی‌شود."
            await ans(msg, r["capped"])
    elif d.startswith("cc:"):
        _, act, pid = d.split(":")
        pid = int(pid)
        cur = await db.cart_qty(uid, pid)
        new = cur + 1 if act == "inc" else cur - 1 if act == "dec" else 0
        _, capped = await db.cart_set(uid, pid, new)
        await ans("به سقف موجودی رسیدید" if capped and act == "inc" else None)
        body, kb = await cart_view(uid)
        await show(q, body, kb)
    elif d == "cart_clear":
        await db.cart_clear(uid)
        await show(q, "🗑 سبد خالی شد.")
    elif d == "coupon":
        c.user_data["awaiting"] = {"t": "coupon"}
        await q.message.reply_text("🎟 کد تخفیف را بفرستید:")
    elif d == "coupon_off":
        await db.remove_coupon(uid)
        body, kb = await cart_view(uid)
        await show(q, body, kb)
    elif d == "checkout":
        s = await db.cart_summary(uid)
        if not s["items"]:
            await q.message.reply_text("🛒 سبد خالی است.")
            return
        bad = [i for i in s["items"] if i["stock"] < i["qty"]]
        if bad:
            await q.message.reply_text("⚠️ موجودی این کالاها کم شده؛ سبد را اصلاح کنید:\n" + "\n".join(f"• {i['name']} (موجود: {i['stock']})" for i in bad))
            return
        c.user_data["await_loc"] = True
        await q.message.reply_text("📍 برای ثبت سفارش، موقعیت مکانی‌تان را با دکمه‌ی زیر بفرستید:", reply_markup=LOC_KB)
    elif d.startswith("rev:"):
        pid = int(d[4:])
        avg, cnt = await db.rating(pid)
        com = await db.recent_comments(pid)
        lines = [f"⭐ امتیاز: <b>{avg}/5</b> ({cnt} نظر)"]
        for r in com:
            lines.append(f"\n{'⭐' * r['rating']}\n{esc(r['comment'])}")
        await q.message.reply_text("\n".join(lines), parse_mode=HTML, reply_markup=IM([[IB("✍️ ثبت امتیاز و نظر", callback_data=f"rate:{pid}")]]))
    elif d.startswith("rate:"):
        pid = int(d[5:])
        await q.message.reply_text("امتیازتان را انتخاب کنید:", reply_markup=IM([[IB("⭐" * n, callback_data=f"rt:{pid}:{n}") for n in (1, 2, 3, 4, 5)]]))
    elif d.startswith("rt:"):
        _, pid, n = d.split(":")
        await db.add_rating(int(pid), uid, max(1, min(5, int(n))))
        c.user_data["awaiting"] = {"t": "comment", "pid": int(pid)}
        await show(q, f"✅ امتیاز {n} ثبت شد.\n\nاگر می‌خواهید نظر متنی هم بنویسید همین حالا بفرستید (یا «-» برای رد کردن).")


# ====================== پنل ادمین ======================
def admin_menu_kb():
    return IM([
        [IB("➕ افزودن محصول", callback_data="a:add"), IB("🔎 ویرایش / حذف", callback_data="a:find")],
        [IB("📥 آپلود اکسل", callback_data="a:xls"), IB("📤 خروجی اکسل", callback_data="a:export")],
        [IB("🧾 سفارش‌ها", callback_data="a:orders"), IB("📊 آمار", callback_data="a:stats")],
        [IB("🎟 کوپن‌ها", callback_data="a:coupons"), IB("📈 تخفیف پله‌ای", callback_data="a:tiers")],
        [IB("🚚 هزینه ارسال", callback_data="a:ship"), IB("⚠️ آستانه‌ی موجودی کم", callback_data="a:low")],
        [IB("📢 پیام همگانی", callback_data="a:bc"), IB("📣 پست در کانال/گروه", callback_data="a:post")],
        [IB("📡 کانال‌ها و گروه‌ها", callback_data="a:ch")],
    ])


async def cmd_admin(u, c):
    if not is_owner(u):
        await u.message.reply_text("⛔")
        return
    c.user_data.pop("flow", None)
    await u.message.reply_text("👑 <b>پنل مدیریت ماهلا</b>", parse_mode=HTML, reply_markup=admin_menu_kb())


def admin_card(p):
    has_photo = "دارد" if p.get("photo") else "ندارد"
    text = (f"⚙️ <b>مدیریت محصول</b>\n\n🔖 کد: <code>{esc(p['code'])}</code>\n🛍 {esc(p['name'])}\n🏷 برند: {esc(p['brand'] or '-')}\n"
            f"📂 دسته: {esc(p['category'])}\n💰 قیمت: {money(p['price'])} تومان\n📦 موجودی: {p['stock']}\n🖼 عکس: {has_photo}")
    pid = p["id"]
    kb = IM([
        [IB("نام", callback_data=f"a:ep:{pid}:name"), IB("برند", callback_data=f"a:ep:{pid}:brand"), IB("دسته", callback_data=f"a:ep:{pid}:category")],
        [IB("قیمت", callback_data=f"a:ep:{pid}:price"), IB("موجودی", callback_data=f"a:ep:{pid}:stock"), IB("کد", callback_data=f"a:ep:{pid}:code")],
        [IB("توضیحات", callback_data=f"a:ep:{pid}:description"), IB("🖼 عکس", callback_data=f"a:ep:{pid}:photo")],
        [IB("🗑 حذف محصول", callback_data=f"a:dp:{pid}")],
        [IB("🔙 منو", callback_data="a:menu")],
    ])
    return text, kb


FIELD_FA = {"name": "نام", "brand": "برند", "category": "دسته‌بندی", "price": "قیمت (تومان)", "stock": "موجودی",
            "code": "کد", "description": "توضیحات", "photo": "عکس (بفرستید یا لینک بدهید)"}
BACK_MENU = IM([[IB("🔙 منو", callback_data="a:menu")]])


async def admin_cb(q, c, d):
    if d == "menu":
        c.user_data.pop("flow", None)
        await show(q, "👑 <b>پنل مدیریت ماهلا</b>", admin_menu_kb())
    elif d == "add":
        c.user_data["flow"] = {"t": "add", "i": 0, "data": {}}
        await q.message.reply_text("➕ افزودن محصول (برای لغو /cancel)\n\n" + ADD_STEPS[0][1])
    elif d == "find":
        c.user_data["flow"] = {"t": "find"}
        await q.message.reply_text("🔎 نام یا کد محصول را بفرستید:")
    elif d.startswith("pm:"):
        p = await db.get_product(int(d[3:]))
        if p:
            text, kb = admin_card(p)
            await q.message.reply_text(text, parse_mode=HTML, reply_markup=kb)
    elif d.startswith("ep:"):
        _, pid, field = d.split(":")
        c.user_data["flow"] = {"t": "edit", "pid": int(pid), "field": field}
        await q.message.reply_text(f"✏️ مقدار جدید «{FIELD_FA[field]}» را بفرستید (لغو: /cancel):")
    elif d.startswith("dp:"):
        pid = d[3:]
        await show(q, "⚠️ این محصول حذف شود؟", IM([[IB("✅ بله، حذف کن", callback_data=f"a:dpy:{pid}"), IB("❌ خیر", callback_data=f"a:pm:{pid}")]]))
    elif d.startswith("dpy:"):
        await db.delete_product(int(d[4:]))
        await show(q, "🗑 محصول حذف شد.", BACK_MENU)
    elif d == "xls":
        await q.message.reply_text(
            "📥 <b>آپلود اکسل</b>\n\nفایل اکسل (xlsx / xls) یا CSV را همین‌جا بفرستید؛ خودکار خوانده می‌شود:\n"
            "• هر شکل جدولی، هر ترتیب ستون، عنوان‌ها فارسی یا انگلیسی\n• ردیف عنوان لازم نیست بالای فایل باشد (بالایش می‌تواند تیتر باشد)\n"
            "• ستون‌های اضافه (مثل نوع پوست) به‌عنوان مشخصات محصول ذخیره می‌شوند\n• چند برگه (Sheet) پشتیبانی می‌شود؛ ردیف تک‌ستونی = عنوان دسته\n"
            "• محصول موجود (هم‌کد یا هم‌نام) بروزرسانی می‌شود\n• اگر زیر فایل بنویسید «جایگزین»، محصولاتی که در فایل نیستند حذف می‌شوند\n"
            "• عکس: در ستونی به نام «لینک عکس» آدرس اینترنتی بگذارید، یا عکس را با توضیح = کد محصول برای من بفرستید (چند عکس هم‌زمان مجاز است).\n\n"
            "💡 «📤 خروجی اکسل» فایل فعلی را می‌دهد تا ویرایش و دوباره آپلود کنید.", parse_mode=HTML)
    elif d == "export":
        prods = await db.all_products()
        if not prods:
            await q.message.reply_text("محصولی ثبت نشده.")
            return
        data = await asyncio.to_thread(export_xlsx, prods)
        await q.message.reply_document(document=data, filename="mahla-products.xlsx", caption=f"📤 {len(prods)} محصول")
    elif d == "orders":
        orders = await db.admin_orders()
        if not orders:
            await show(q, "سفارشی ثبت نشده.", BACK_MENU)
            return
        rows = [[IB(f"{STATUS_ICON.get(o['status'], '')} #{o['id']} • {money(o['total'])} • {o['status']}", callback_data=f"a:o:{o['id']}")] for o in orders]
        rows.append([IB("🔙 منو", callback_data="a:menu")])
        await show(q, "🧾 <b>سفارش‌ها</b> (فعال‌ها اول)", IM(rows))
    elif d.startswith("o:"):
        o = await db.get_order(int(d[2:]))
        if o:
            await show(q, order_text(o, True), status_kb(o))
            if o["lat"] is not None:
                await c.bot.send_location(OWNER_ID, latitude=o["lat"], longitude=o["lon"])
    elif d.startswith("st:"):
        _, oid, i = d.split(":")
        o = await db.set_order_status(int(oid), STATUSES[int(i)])
        if not o:
            return
        if not o["changed"]:
            await q.message.reply_text("ℹ️ وضعیت تغییری نکرد (سفارش لغوشده قابل تغییر نیست).")
            return
        await show(q, order_text(o, True), status_kb(o))
        try:
            await c.bot.send_message(o["user_id"], f"🔔 وضعیت سفارش #{o['id']} شما: {STATUS_ICON[o['status']]} <b>{esc(o['status'])}</b>", parse_mode=HTML)
        except TelegramError:
            await q.message.reply_text("⚠️ پیام وضعیت به مشتری نرسید (ربات را بلاک کرده).")
    elif d == "stats":
        s = await db.stats()
        await show(q, f"📊 <b>آمار</b>\n\n📦 محصولات: {s['products']} (ناموجود: {s['out']})\n👥 کاربران: {s['users']}\n🧾 سفارش‌ها: {s['orders']} (در انتظار: {s['pending']})\n💰 مجموع فروش (بدون لغوشده‌ها): {money(s['revenue'])} تومان", BACK_MENU)
    elif d == "coupons":
        cps = await db.list_coupons()
        lines = ["🎟 <b>کوپن‌ها</b>\n"] + [f"<code>{esc(x['code'])}</code> — {x['percent']}٪ — استفاده: {x['used']}/{x['max_uses'] or '∞'}" for x in cps]
        rows = [[IB(f"🗑 {x['code']}", callback_data=f"a:cpdel:{x['code']}")] for x in cps]
        rows += [[IB("➕ کوپن جدید", callback_data="a:cpadd")], [IB("🔙 منو", callback_data="a:menu")]]
        await show(q, "\n".join(lines) if cps else "🎟 هنوز کوپنی نیست.", IM(rows))
    elif d == "cpadd":
        c.user_data["flow"] = {"t": "coupon"}
        await q.message.reply_text("🎟 فرمت: <code>کد|درصد|حداکثر تعداد استفاده</code>\nمثال: <code>MAHLA10|10|50</code> (تعداد اختیاری؛ خالی = نامحدود)", parse_mode=HTML)
    elif d.startswith("cpdel:"):
        await db.del_coupon(d[6:])
        await show(q, "🗑 کوپن حذف شد.", BACK_MENU)
    elif d == "tiers":
        ts = await db.list_tiers()
        lines = ["📈 <b>تخفیف پله‌ای خودکار</b>\n"] + [f"از {money(x['min_amount'])} تومان به بالا → {x['percent']}٪" for x in ts]
        rows = [[IB(f"🗑 {money(x['min_amount'])}", callback_data=f"a:tdel:{x['min_amount']}")] for x in ts]
        rows += [[IB("➕ پله‌ی جدید", callback_data="a:tadd")], [IB("🔙 منو", callback_data="a:menu")]]
        await show(q, "\n".join(lines) if ts else "📈 هنوز پله‌ای تعریف نشده.", IM(rows))
    elif d == "tadd":
        c.user_data["flow"] = {"t": "tier"}
        await q.message.reply_text("📈 فرمت: <code>مبلغ|درصد</code>\nمثال: <code>500000|5</code> یعنی بالای ۵۰۰ هزار تومان ۵٪ تخفیف", parse_mode=HTML)
    elif d.startswith("tdel:"):
        await db.del_tier(int(d[5:]))
        await show(q, "🗑 پله حذف شد.", BACK_MENU)
    elif d == "ship":
        c.user_data["flow"] = {"t": "ship"}
        cur = await db.get_setting("shipping_fee", "0")
        await q.message.reply_text(f"🚚 هزینه‌ی ارسال فعلی: {money(cur)} تومان\nمبلغ جدید را بفرستید (۰ = رایگان):")
    elif d == "low":
        c.user_data["flow"] = {"t": "low"}
        cur = await db.get_setting("low_stock", "5")
        await q.message.reply_text(f"⚠️ وقتی موجودی محصولی بعد از سفارش به این عدد یا کمتر برسد هشدار می‌گیرید. فعلی: {cur}\nعدد جدید را بفرستید:")
    elif d in ("bc", "post"):
        chans = json.loads(await db.get_setting("channels", "[]"))
        if d == "post" and not chans:
            await q.message.reply_text("ابتدا از «📡 کانال‌ها و گروه‌ها» یک کانال/گروه اضافه کنید.")
            return
        c.user_data["flow"] = {"t": d}
        what = "همه‌ی کاربران ربات" if d == "bc" else "کانال‌ها/گروه‌های ثبت‌شده"
        await q.message.reply_text(f"📢 پیام (متن، عکس، ویدیو، ...) را بفرستید تا برای {what} ارسال شود. (لغو: /cancel)")
    elif d in ("bcok", "bcno"):
        pend = c.user_data.pop("pending_send", None)
        if d == "bcno" or not pend:
            await show(q, "❌ ارسال لغو شد.")
            return
        await show(q, "⏳ ارسال شروع شد؛ نتیجه را اعلام می‌کنم.")
        c.application.create_task(do_send(c.bot, pend))
    elif d == "ch":
        chans = json.loads(await db.get_setting("channels", "[]"))
        lines = ["📡 <b>کانال‌ها و گروه‌ها</b>\n(ربات باید در آن‌ها ادمین باشد)\n"] + [f"• {esc(x['title'])}" for x in chans]
        rows = [[IB(f"🗑 {x['title'][:30]}", callback_data=f"a:chdel:{x['id']}")] for x in chans]
        rows += [[IB("➕ افزودن", callback_data="a:chadd")], [IB("🔙 منو", callback_data="a:menu")]]
        await show(q, "\n".join(lines), IM(rows))
    elif d == "chadd":
        c.user_data["flow"] = {"t": "chadd"}
        await q.message.reply_text("آی‌دی کانال/گروه را بفرستید؛ مثل <code>@mychannel</code> یا <code>-1001234567890</code>\n(اول ربات را در آن ادمین کنید)", parse_mode=HTML)
    elif d.startswith("chdel:"):
        chans = [x for x in json.loads(await db.get_setting("channels", "[]")) if str(x["id"]) != d[6:]]
        await db.set_setting("channels", json.dumps(chans, ensure_ascii=False))
        await show(q, "🗑 حذف شد.", BACK_MENU)


ADD_STEPS = [
    ("code", "🔖 کد محصول را بفرستید (یا - برای کد خودکار):"),
    ("name", "🛍 نام محصول:"),
    ("brand", "🏷 برند (یا -):"),
    ("category", "📂 دسته‌بندی:"),
    ("price", "💰 قیمت به تومان:"),
    ("stock", "📦 موجودی انبار:"),
    ("description", "📝 توضیحات (یا -):"),
    ("photo", "🖼 عکس محصول را بفرستید (یا - برای رد کردن):"),
]


def _photo_value(m, txt):
    if m.photo:
        return m.photo[-1].file_id
    if txt.lower().startswith("http"):
        return txt
    return None


async def owner_flow(u, c, flow):
    m = u.message
    txt = (m.text or m.caption or "").strip()
    t = flow["t"]

    if t == "add":
        key = ADD_STEPS[flow["i"]][0]
        val = None
        if key == "photo":
            val = _photo_value(m, txt)
            if val is None and txt != "-":
                await m.reply_text("عکس بفرستید، لینک بدهید یا - بزنید.")
                return
        elif not txt:
            await m.reply_text("متن بفرستید.")
            return
        elif key in ("price", "stock"):
            val = to_int(txt)
            if val is None or val < 0:
                await m.reply_text("فقط عدد بفرستید.")
                return
        elif key == "code":
            val = None if txt == "-" else ncode(txt)
        elif key == "name":
            if txt == "-":
                await m.reply_text("نام لازم است.")
                return
            val = txt
        elif key == "category":
            val = "سایر" if txt == "-" else txt
        else:
            val = None if txt == "-" else txt
        flow["data"][key] = val
        flow["i"] += 1
        if flow["i"] < len(ADD_STEPS):
            await m.reply_text(ADD_STEPS[flow["i"]][1])
            return
        c.user_data.pop("flow", None)
        res, p = await db.save_product(flow["data"])
        text, kb = admin_card(p)
        await m.reply_text(("✅ محصول جدید ذخیره شد." if res == "added" else "✅ محصول با این کد بروزرسانی شد.") + "\n\n" + text, parse_mode=HTML, reply_markup=kb)

    elif t == "edit":
        field = flow["field"]
        if field == "photo":
            val = _photo_value(m, txt)
            if val is None:
                await m.reply_text("عکس بفرستید یا لینک بدهید.")
                return
        elif field in ("price", "stock"):
            val = to_int(txt)
            if val is None or val < 0:
                await m.reply_text("فقط عدد بفرستید.")
                return
        elif not txt:
            await m.reply_text("متن بفرستید.")
            return
        else:
            val = ncode(txt) if field == "code" else txt
        r = await db.update_field(flow["pid"], field, val)
        if r == "dup":
            await m.reply_text("❌ این کد قبلاً برای محصول دیگری ثبت شده. کد دیگری بفرستید:")
            return
        c.user_data.pop("flow", None)
        p = await db.get_product(flow["pid"])
        text, kb = admin_card(p)
        await m.reply_text("✅ ذخیره شد.\n\n" + text, parse_mode=HTML, reply_markup=kb)

    elif t == "find":
        c.user_data.pop("flow", None)
        res = await db.search(txt) if txt else []
        if not res:
            await m.reply_text("❌ پیدا نشد.", reply_markup=BACK_MENU)
        elif len(res) == 1:
            text, kb = admin_card(res[0])
            await m.reply_text(text, parse_mode=HTML, reply_markup=kb)
        else:
            await m.reply_text("کدام محصول؟", reply_markup=IM([[IB(f"{p['name'][:30]} ({p['code']})", callback_data=f"a:pm:{p['id']}")] for p in res[:12]]))

    elif t == "coupon":
        parts = [x for x in re.split(r"[|\s,،]+", txt) if x]
        try:
            code, pct = ncode(parts[0]), int(to_int(parts[1]))
            mx = to_int(parts[2], 0) if len(parts) > 2 else 0
            assert 1 <= pct <= 100 and mx >= 0
        except (IndexError, TypeError, AssertionError):
            await m.reply_text("❌ فرمت: کد|درصد|حداکثر تعداد\nمثال: MAHLA10|10|50")
            return
        c.user_data.pop("flow", None)
        await db.add_coupon(code, pct, mx)
        await m.reply_text(f"✅ کوپن {code} ({pct}٪) ذخیره شد.", reply_markup=BACK_MENU)

    elif t == "tier":
        parts = [x for x in re.split(r"[|\s,،]+", txt) if x]
        try:
            amt, pct = to_int(parts[0]), to_int(parts[1])
            assert amt and amt > 0 and pct and 1 <= pct <= 100
        except (IndexError, TypeError, AssertionError):
            await m.reply_text("❌ فرمت: مبلغ|درصد\nمثال: 500000|5")
            return
        c.user_data.pop("flow", None)
        await db.add_tier(amt, pct)
        await m.reply_text(f"✅ بالای {money(amt)} تومان → {pct}٪ تخفیف", reply_markup=BACK_MENU)

    elif t in ("ship", "low"):
        v = to_int(txt)
        if v is None or v < 0:
            await m.reply_text("فقط عدد بفرستید.")
            return
        c.user_data.pop("flow", None)
        await db.set_setting("shipping_fee" if t == "ship" else "low_stock", v)
        await m.reply_text("✅ ذخیره شد.", reply_markup=BACK_MENU)

    elif t == "chadd":
        ref = txt if txt.lstrip("-").isdigit() is False else int(txt)
        try:
            chat = await c.bot.get_chat(ref)
        except TelegramError:
            await m.reply_text("❌ پیدا نشد. مطمئن شوید آی‌دی درست است و ربات در آن ادمین است.")
            return
        chans = json.loads(await db.get_setting("channels", "[]"))
        if all(x["id"] != chat.id for x in chans):
            chans.append({"id": chat.id, "title": chat.title or chat.username or str(chat.id)})
            await db.set_setting("channels", json.dumps(chans, ensure_ascii=False))
        c.user_data.pop("flow", None)
        await m.reply_text(f"✅ «{chat.title or chat.username}» اضافه شد.", reply_markup=BACK_MENU)

    elif t in ("bc", "post"):
        c.user_data.pop("flow", None)
        if t == "bc":
            targets = await db.user_ids()
        else:
            targets = [x["id"] for x in json.loads(await db.get_setting("channels", "[]"))]
        c.user_data["pending_send"] = {"chat": m.chat_id, "msg": m.message_id, "targets": targets, "kind": t}
        await m.reply_text(f"این پیام برای {len(targets)} {'نفر' if t == 'bc' else 'کانال/گروه'} ارسال شود؟",
                           reply_markup=IM([[IB("✅ ارسال", callback_data="a:bcok"), IB("❌ لغو", callback_data="a:bcno")]]))


async def do_send(bot, pend):
    sent = failed = 0
    for tid in pend["targets"]:
        for attempt in range(2):
            try:
                await bot.copy_message(tid, pend["chat"], pend["msg"])
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Forbidden:
                if pend["kind"] == "bc":
                    await db.deactivate_user(tid)
                failed += 1
                break
            except TelegramError:
                failed += 1
                break
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(OWNER_ID, f"📢 ارسال تمام شد.\n✅ موفق: {sent}\n❌ ناموفق: {failed}")
    except TelegramError:
        pass


async def owner_photo(u, c):
    m = u.message
    code = (m.caption or "").strip()
    if not code:
        await m.reply_text("برای ست کردن عکس محصول، عکس را با توضیح = کد محصول بفرستید.")
        return
    n = await db.set_photo(code, m.photo[-1].file_id)
    await m.reply_text(f"🖼 عکس محصول {ncode(code)} ذخیره شد." if n else f"❌ محصولی با کد {ncode(code)} پیدا نشد.")


async def import_excel(u, c):
    m = u.message
    doc = m.document
    name = doc.file_name or "file"
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext not in ("xlsx", "xlsm", "xls", "csv", "tsv", "txt"):
        await m.reply_text("فقط فایل اکسل (xlsx/xls) یا CSV پشتیبانی می‌شود.")
        return
    status = await m.reply_text("⏳ در حال خواندن فایل...")
    try:
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        res = await asyncio.to_thread(parse_file, data, name)
    except ValueError as e:
        await status.edit_text(f"❌ {e}")
        return
    except Exception as e:
        log.exception("خواندن اکسل ناموفق")
        await status.edit_text(f"❌ فایل خوانده نشد ({type(e).__name__}). مطمئن شوید فایل سالم است.")
        return
    if not res["products"]:
        lines = ["❌ محصولی پیدا نشد."] + res["notes"]
        lines.append("حداقل دو ستون «نام» و «قیمت» لازم است.")
        await status.edit_text("\n".join(lines))
        return
    replace = "جایگزین" in (m.caption or "")
    r = await db.bulk_upsert(res["products"], replace=replace)
    lines = [f"✅ <b>{len(res['products'])} محصول پردازش شد</b>", f"🆕 جدید: {r['added']} | 🔄 بروزرسانی: {r['updated']}"]
    if replace:
        lines.append(f"🗑 حذف‌شده (نبودند در فایل): {r['removed']}")
    lines.append("\n🧭 ستون‌های شناسایی‌شده:\n" + "\n".join(esc(x) for x in res["mapping"]))
    if res["extras"]:
        lines.append("📋 ستون‌های اضافه (به‌عنوان مشخصات): " + esc("، ".join(res["extras"])))
    if res["stock_missing"]:
        lines.append(f"⚠️ ستون موجودی نبود؛ محصولات جدید با موجودی {db.DEFAULT_STOCK} ثبت شدند.")
    lines += [esc(x) for x in res["notes"]]
    if res["skipped"]:
        lines.append(f"\n⚠️ {len(res['skipped'])} ردیف رد شد:")
        lines += [f"• ردیف {no}: {esc(why)}" for no, why in res["skipped"][:8]]
        if len(res["skipped"]) > 8:
            lines.append(f"• و {len(res['skipped']) - 8} مورد دیگر")
    await status.edit_text("\n".join(lines)[:4000], parse_mode=HTML)


# ====================== اجرا ======================
async def on_error(u, c):
    err = c.error
    if isinstance(err, NetworkError):
        log.warning("خطای شبکه (خودکار دوباره تلاش می‌شود): %s", err)
        return
    log.error("خطای پیش‌بینی‌نشده", exc_info=err)
    try:
        if isinstance(u, Update) and u.effective_message:
            await u.effective_message.reply_text("⚠️ خطایی رخ داد؛ لطفاً دوباره تلاش کنید.")
    except TelegramError:
        pass


def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ متغیر BOT_TOKEN تنظیم نشده!")
    db.init_sync()
    log.info("دیتابیس: %s", db.DB_PATH)
    app = (ApplicationBuilder().token(BOT_TOKEN)
           .concurrent_updates(True)
           .connect_timeout(15).read_timeout(25).write_timeout(25).pool_timeout(15)
           .get_updates_connect_timeout(15).get_updates_read_timeout(35).get_updates_pool_timeout(15)
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    log.info("✅ ربات آماده است")
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
