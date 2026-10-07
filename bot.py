import asyncio
import hashlib
import io
import json
import logging
import os
import re
from datetime import datetime

import requests
from telegram import (
    InlineKeyboardButton as IB,
    InlineKeyboardMarkup as IM,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
STATUS_ICON = {
    STATUSES[0]: "⏳",
    STATUSES[1]: "🔄",
    STATUSES[2]: "🚚",
    STATUSES[3]: "✅",
    STATUSES[4]: "❌",
}

# ====================== تنظیمات ارتباط با ایجنت دیفای (Dify) ======================
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = os.getenv("DIFY_API_URL", "https://api.dify.ai/v1/chat-messages")

BTN_ADMIN_PANEL = "👑 پنل مدیریت"
BTN_CATS, BTN_CODE, BTN_CART, BTN_ORDERS, BTN_CONTACT = (
    "📦 دسته‌بندی‌ها",
    "🔍 جستجو / استعلام کد",
    "🛒 سبد خرید",
    "🧾 سفارش‌ها",
    "📞 ارتباط با ادمین",
)
BTN_CANCEL = "❌ انصراف"

MENU_TEXTS = {
    BTN_ADMIN_PANEL,
    BTN_CATS,
    BTN_CODE,
    BTN_CART,
    BTN_ORDERS,
    BTN_CONTACT,
    BTN_CANCEL,
}

MAIN_KB_USER = ReplyKeyboardMarkup(
    [[BTN_CATS, BTN_CART], [BTN_CODE, BTN_ORDERS], [BTN_CONTACT]],
    resize_keyboard=True,
)
MAIN_KB_ADMIN = ReplyKeyboardMarkup(
    [[BTN_ADMIN_PANEL], [BTN_CATS, BTN_CART], [BTN_CODE, BTN_ORDERS], [BTN_CONTACT]],
    resize_keyboard=True,
)
LOC_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 ارسال موقعیت", request_location=True)], [BTN_CANCEL]],
    resize_keyboard=True,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("shop")


def _ask_dify_sync(query: str, user_id: int) -> str:
    """ارسال همگام درخواست به ایجنت Dify"""
    if not DIFY_API_KEY:
        return "❌ کالایی مطابق جستجو یافت نشد. نام یا کد را بازبینی کرده یا از «دسته‌بندی‌ها» استفاده نمایید."
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "inputs": {},
        "query": query,
        "response_mode": "blocking",
        "conversation_id": "",
        "user": str(user_id),
    }
    try:
        r = requests.post(DIFY_API_URL, json=payload, headers=headers, timeout=25)
        data = r.json()
        return data.get("answer") or "متأسفانه پاسخی دریافت نشد."
    except Exception as e:
        log.warning("خطا در ارتباط با دیفای: %s", e)
        return "در حال حاضر سیستم پاسخگویی هوشمند در دسترس نیست؛ لطفاً کمی بعد امتحان فرمایید."


async def ask_dify(query: str, user_id: int) -> str:
    """ارسال ناهمگام به دیفای جهت جلوگیری از بلاک شدن پردازش ربات"""
    return await asyncio.to_thread(_ask_dify_sync, query, user_id)


def get_main_kb(uid: int) -> ReplyKeyboardMarkup:
    if db.is_admin(uid) or db.is_owner(uid):
        return MAIN_KB_ADMIN
    return MAIN_KB_USER


def chash(name: str) -> str:
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:8]


async def show(q, text: str, kb=None):
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await q.message.reply_text(text, parse_mode=HTML, reply_markup=kb)


# ====================== نمایش کاتالوگ و محصولات ======================
def caption(p: dict, rate: tuple) -> str:
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
        lines.append(
            "\n📋 "
            + " | ".join(f"{esc(k)}: {esc(v)}" for k, v in list(extra.items())[:6])[
                :300
            ]
        )
    lines.append(f"\n💰 <b>{money(p['price'])} تومان</b>")
    return "\n".join(lines)[:1000]


def product_kb(p: dict, n: int = 1, admin_user: bool = False) -> IM:
    pid = p["id"]
    rows = []
    if p["stock"] > 0:
        rows.append(
            [
                IB("➖", callback_data=f"q:{pid}:{n - 1}"),
                IB(f"{n}", callback_data="noop"),
                IB("➕", callback_data=f"q:{pid}:{n + 1}"),
            ]
        )
        rows.append(
            [IB(f"🛒 افزودن {n} عدد به سبد", callback_data=f"add:{pid}:{n}")]
        )
    rows.append([IB("⭐ امتیاز و نظرات", callback_data=f"rev:{pid}")])
    if admin_user:
        rows.append([IB("⚙️ مدیریت این محصول", callback_data=f"a:pm:{pid}")])
    rows.append([IB("🔙 دسته‌بندی‌ها", callback_data="cats")])
    return IM(rows)


async def send_product(msg, p: dict, admin_user: bool = False):
    cap = caption(p, await db.rating(p["id"]))
    kb = product_kb(p, 1, admin_user)
    if p.get("photo"):
        try:
            await msg.reply_photo(
                photo=p["photo"], caption=cap, parse_mode=HTML, reply_markup=kb
            )
            return
        except TelegramError as e:
            log.warning("خطا در ارسال تصویر کالا %s: %s", p["code"], e)
    await msg.reply_text(cap, parse_mode=HTML, reply_markup=kb)


async def cats_kb() -> IM:
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


async def cat_page(h: str, page: int):
    cat = next((c for c, _ in await db.categories() if chash(c) == h), None)
    if cat is None:
        return "دسته پیدا نشد.", await cats_kb()
    prods = await db.products_in(cat)
    tp = max(1, -(-len(prods) // PAGE_SIZE))
    page = max(0, min(page, tp - 1))
    lines = [f"📦 <b>{esc(cat)}</b> — {len(prods)} محصول (صفحه {page + 1}/{tp})\n"]
    btns = []
    for p in prods[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
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


# ====================== پردازش سبد خرید و ثبت سفارش ======================
async def cart_view(uid: int):
    s = await db.cart_summary(uid)
    if not s["items"]:
        return "🛒 سبد خرید خالی است.", None
    lines = ["🛒 <b>سبد خرید شما</b>\n"]
    rows = []
    for i, it in enumerate(s["items"], 1):
        warn = f" ⚠️ فقط {it['stock']} موجود" if it["stock"] < it["qty"] else ""
        lines.append(
            f"{i}. {esc(it['name'])} × {it['qty']} = {money(it['price'] * it['qty'])} تومان{warn}"
        )
        rows.append(
            [
                IB("➖", callback_data=f"cc:dec:{it['pid']}"),
                IB(f"{i}) ×{it['qty']}", callback_data="noop"),
                IB("➕", callback_data=f"cc:inc:{it['pid']}"),
                IB("🗑", callback_data=f"cc:del:{it['pid']}"),
            ]
        )
    lines.append(f"\n💵 جمع خرید: {money(s['subtotal'])} تومان")
    if s["discount"]:
        lines.append(f"🎁 {esc(s['note'])}: -{money(s['discount'])} تومان")
    if s["shipping"]:
        lines.append(f"🚚 هزینه ارسال: {money(s['shipping'])} تومان")
    lines.append(f"✅ <b>مبلغ قابل پرداخت: {money(s['total'])} تومان</b>")
    if s.get("coupon_warn"):
        lines.append(f"\n⚠️ {esc(s['coupon_warn'])}")
    nt = s["next_tier"]
    if nt:
        lines.append(
            f"\n💡 با {money(nt['min_amount'] - s['subtotal'])} تومان خرید بیشتر، {nt['percent']}٪ تخفیف پلکانی دریافت می‌کنید."
        )
    rows.append(
        [
            IB(
                "🎟 حذف کوپن" if s["coupon"] else "🎟 کد تخفیف دارم",
                callback_data="coupon_off" if s["coupon"] else "coupon",
            )
        ]
    )
    rows.append([IB("🧾 نهایی‌سازی سفارش", callback_data="checkout")])
    rows.append([IB("🗑 خالی کردن سبد", callback_data="cart_clear")])
    return "\n".join(lines), IM(rows)


def order_text(o: dict, admin_mode: bool = False) -> str:
    items = json.loads(o["items"])
    lines = [
        f"🧾 <b>سفارش #{o['id']}</b> — {STATUS_ICON.get(o['status'], '')} {esc(o['status'])}",
        f"🕒 {o['created_at']}",
    ]
    if admin_mode:
        who = esc(o["name"] or "-") + (
            f" (@{esc(o['username'])})" if o["username"] else ""
        )
        lines.append(
            f"👤 {who} — <a href=\"tg://user?id={o['user_id']}\">پیام به مشتری</a>"
        )
    lines.append("")
    for it in items:
        lines.append(
            f"• {esc(it['name'])} × {it['qty']} = {money(it['price'] * it['qty'])} تومان"
        )
    lines.append(f"\n💵 جمع کل: {money(o['subtotal'])} تومان")
    if o["discount"]:
        lines.append(f"🎁 {esc(o['discount_note'] or 'تخفیف')}: -{money(o['discount'])} تومان")
    if o["shipping"]:
        lines.append(f"🚚 هزینه ارسال: {money(o['shipping'])} تومان")
    lines.append(f"✅ <b>مبلغ نهایی: {money(o['total'])} تومان</b>")
    return "\n".join(lines)


def status_kb(o: dict) -> IM:
    btns = [
        IB(f"{STATUS_ICON[s]} {s}", callback_data=f"a:st:{o['id']}:{i}")
        for i, s in enumerate(STATUSES)
    ]
    rows = [btns[i : i + 2] for i in range(0, len(btns), 2)]
    rows.append([IB("🔙 لیست سفارش‌ها", callback_data="a:orders")])
    return IM(rows)


async def cmd_orders(u: Update, c: ContextTypes.DEFAULT_TYPE):
    orders = await db.user_orders(u.effective_user.id)
    if not orders:
        await u.effective_message.reply_text("🧾 هنوز سفارشی ثبت نکرده‌اید.")
        return
    lines = ["🧾 <b>سفارش‌های شما</b>\n"]
    for o in orders:
        lines.append(
            f"{STATUS_ICON.get(o['status'], '')} #{o['id']} — {money(o['total'])} تومان — <b>{esc(o['status'])}</b>\n<i>{o['created_at']}</i>\n"
        )
    await u.effective_message.reply_text("\n".join(lines), parse_mode=HTML)


async def on_location(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not c.user_data.get("await_loc"):
        await u.message.reply_text(
            "برای ثبت سفارش، از سبد خرید روی «نهایی‌سازی سفارش» بزنید.",
            reply_markup=get_main_kb(uid),
        )
        return
    user, loc = u.effective_user, u.message.location
    res = await db.create_order(
        user.id, user.username or "", user.full_name, loc.latitude, loc.longitude
    )
    if res["status"] == "empty":
        c.user_data.pop("await_loc", None)
        await u.message.reply_text("🛒 سبد خرید خالی است.", reply_markup=get_main_kb(uid))
        return
    if res["status"] == "stock":
        names = "\n".join(
            f"• {esc(i['name'])} (موجودی: {i['stock']}، در سبد: {i['qty']})"
            for i in res["items"]
        )
        c.user_data.pop("await_loc", None)
        await u.message.reply_text(
            f"⚠️ موجودی این کالاها کافی نیست:\n{names}\n\nسبد را اصلاح کنید.",
            parse_mode=HTML,
            reply_markup=get_main_kb(uid),
        )
        body, kb = await cart_view(user.id)
        await u.message.reply_text(body, parse_mode=HTML, reply_markup=kb)
        return

    c.user_data.pop("await_loc", None)
    o = res["order"]
    await u.message.reply_text(
        f"✅ <b>سفارش #{o['id']} با موفقیت ثبت شد!</b>\n💵 مبلغ: {money(o['total'])} تومان\n\nوضعیت سفارش را از «🧾 سفارش‌ها» دنبال کنید. به زودی با شما تماس می‌گیریم 🙏",
        parse_mode=HTML,
        reply_markup=get_main_kb(uid),
    )

    admin_targets = db.admin_ids_sync()
    for aid in admin_targets:
        try:
            await c.bot.send_message(
                aid,
                "🔔 <b>سفارش جدید دریافت شد</b>\n\n" + order_text(o, True),
                parse_mode=HTML,
                reply_markup=status_kb(o),
            )
            await c.bot.send_location(
                aid, latitude=loc.latitude, longitude=loc.longitude
            )
            for p in res["low"]:
                await c.bot.send_message(
                    aid,
                    f"⚠️ هشدار موجودی کم: {esc(p['name'])} (کد {esc(p['code'])}) — {p['stock']} عدد باقی‌مانده",
                    parse_mode=HTML,
                )
        except TelegramError as e:
            log.error("خطا در ارسال اعلان سفارش به ادمین %s: %s", aid, e)


# ====================== کنترلر رویدادهای متنی و فرامین ======================
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    await db.track_user(uid, u.effective_user.full_name)
    c.user_data["seen"] = True
    c.user_data.pop("flow", None)
    n = await db.count_products()
    role_msg = ""
    if db.is_owner(uid):
        role_msg = "\n👑 شما به عنوان <b>مالک اصلی سامانه</b> شناسایی شدید."
    elif db.is_admin(uid):
        role_msg = "\n⭐️ شما به عنوان <b>ادمین سامانه</b> شناسایی شدید."

    await u.message.reply_text(
        f"🌸 <b>{TAGLINE}</b>\n\n👋 خوش آمدید!\n📦 تعداد محصولات فروشگاه: <b>{n}</b>{role_msg}\n\nنام یا کد کالا را وارد نمایید یا از گزینه‌های زیر استفاده فرمایید.",
        parse_mode=HTML,
        reply_markup=get_main_kb(uid),
    )


async def cmd_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    t = (
        "ℹ️ <b>راهنمای خرید</b>\n\n"
        "• با انتخاب «دسته‌بندی‌ها» کاتالوگ فروشگاه را مرور فرمایید.\n"
        "• نام یا کد محصول را برای جستجو مستقیم بفرستید.\n"
        "• در سبد خرید، سفارش را نهایی کرده و موقعیت مکانی تحویل را ارسال کنید."
    )
    if db.is_admin(uid) or db.is_owner(uid):
        t += "\n\n👑 برای ورود به پنل، روی دکمه «👑 پنل مدیریت» در کیبورد بزنید یا از دستور /admin استفاده کنید."
    await u.message.reply_text(t, parse_mode=HTML)


async def cmd_cancel(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    c.user_data.pop("flow", None)
    c.user_data.pop("await_loc", None)
    c.user_data.pop("awaiting", None)
    await u.message.reply_text("❌ عملیات لغو شد.", reply_markup=get_main_kb(uid))


async def on_message(u: Update, c: ContextTypes.DEFAULT_TYPE):
    m = u.message
    if not m or not u.effective_user:
        return
    uid = u.effective_user.id
    if not c.user_data.get("seen"):
        await db.track_user(uid, u.effective_user.full_name)
        c.user_data["seen"] = True

    if m.location:
        return await on_location(u, c)

    if db.is_admin(uid) or db.is_owner(uid):
        flow = c.user_data.get("flow")
        if flow and (m.text or "").strip() in MENU_TEXTS:
            c.user_data.pop("flow", None)
            flow = None
        if flow:
            return await owner_flow(u, c, flow)
        if m.document:
            return await import_excel(u, c)
        if m.photo and m.caption:
            return await owner_photo(u, c)

    if m.text:
        return await on_text(u, c)


async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    txt = u.message.text.strip()
    uid = u.effective_user.id
    aw = c.user_data.get("awaiting")

    if aw and txt not in MENU_TEXTS:
        c.user_data.pop("awaiting", None)
        if aw["t"] == "coupon":
            res = await db.apply_coupon_ex(uid, txt)
            await u.message.reply_text(res["text"])
            body, kb = await cart_view(uid)
            await u.message.reply_text(body, parse_mode=HTML, reply_markup=kb)
        elif aw["t"] == "comment":
            if txt != "-":
                await db.add_comment(aw["pid"], uid, txt[:500])
            await u.message.reply_text("🙏 نظر شما با سپاس ثبت شد!")
        return

    if txt in MENU_TEXTS:
        c.user_data.pop("awaiting", None)

    if txt == BTN_ADMIN_PANEL:
        if db.is_admin(uid) or db.is_owner(uid):
            await cmd_admin(u, c)
        else:
            await u.message.reply_text("⛔ شما دسترسی ادمین ندارید.")
    elif txt == BTN_CATS:
        await u.message.reply_text("📂 دسته‌بندی‌های کالا:", reply_markup=await cats_kb())
    elif txt == BTN_CART:
        body, kb = await cart_view(uid)
        await u.message.reply_text(body, parse_mode=HTML, reply_markup=kb)
    elif txt == BTN_ORDERS:
        await cmd_orders(u, c)
    elif txt == BTN_CONTACT:
        await u.message.reply_text(
            "📞 پشتیبانی و ارتباط با مدیریت:",
            reply_markup=IM([[IB("💬 پیام به ادمین", url=ADMIN_LINK)]]),
        )
    elif txt == BTN_CODE:
        await u.message.reply_text("🔍 نام یا کد کالا را ارسال فرمایید:")
    elif txt == BTN_CANCEL:
        c.user_data.pop("await_loc", None)
        await u.message.reply_text("❌ عملیات متوقف شد.", reply_markup=get_main_kb(uid))
    else:
        res = await db.search(txt)
        if not res:
            # اگر محصولی پیدا نشد، پیام مستقیم به ایجنت هوشمند Dify ارجاع داده می‌شود
            ai_reply = await ask_dify(txt, uid)
            await u.message.reply_text(ai_reply)
            return

        is_adm = db.is_admin(uid) or db.is_owner(uid)
        for p in res[:3]:
            await send_product(u.message, p, is_adm)
        if len(res) > 3:
            await u.message.reply_text(
                f"🔎 {len(res) - 3} نتیجه منطبق دیگر:",
                reply_markup=IM(
                    [
                        [
                            IB(
                                f"{p['name'][:30]} — {money(p['price'])}",
                                callback_data=f"p:{p['id']}",
                            )
                        ]
                        for p in res[3:13]
                    ]
                ),
            )


# ====================== پردازش کلیک‌های دکمه‌های شیشه‌ای ======================
async def on_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    d = q.data or ""
    uid = q.from_user.id
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
            if not (db.is_admin(uid) or db.is_owner(uid)):
                await ans("⛔ شما دسترسی ادمین ندارید.", True)
                return
            await ans()
            await admin_cb(q, c, d[2:], uid)
        else:
            await user_cb(q, c, d, ans, uid)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    finally:
        await ans()


async def user_cb(q, c, d: str, ans, uid: int):
    admin_user = db.is_admin(uid) or db.is_owner(uid)
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
            await send_product(q.message, p, admin_user)
        else:
            await q.message.reply_text("کالا یافت نشد.")
    elif d.startswith("q:"):
        _, pid, n = d.split(":")
        p = await db.get_product(int(pid))
        if p and p["stock"] > 0:
            n = max(1, min(int(n), p["stock"]))
            await q.edit_message_reply_markup(
                reply_markup=product_kb(p, n, admin_user)
            )
    elif d.startswith("add:"):
        _, pid, n = d.split(":")
        r = await db.cart_add(uid, int(pid), max(1, int(n)))
        if r is None:
            await ans("❌ کالا ناموجود است.", True)
        else:
            msg = f"✅ {r['added']} عدد به سبد افزوده شد (مجموع در سبد: {r['qty']})"
            if r["capped"]:
                msg += "\nتعداد به سقف موجودی انبار محدود گردید."
            await ans(msg, r["capped"])
    elif d.startswith("cc:"):
        _, act, pid = d.split(":")
        pid = int(pid)
        cur = await db.cart_qty(uid, pid)
        new = cur + 1 if act == "inc" else cur - 1 if act == "dec" else 0
        _, capped = await db.cart_set(uid, pid, new)
        await ans("به سقف موجودی رسیدید." if capped and act == "inc" else None)
        body, kb = await cart_view(uid)
        await show(q, body, kb)
    elif d == "cart_clear":
        await db.cart_clear(uid)
        await show(q, "🗑 سبد خرید خالی شد.")
    elif d == "coupon":
        c.user_data["awaiting"] = {"t": "coupon"}
        await q.message.reply_text("🎟 کد تخفیف خود را ارسال فرمایید:")
    elif d == "coupon_off":
        await db.remove_coupon(uid)
        body, kb = await cart_view(uid)
        await show(q, body, kb)
    elif d == "checkout":
        s = await db.cart_summary(uid)
        if not s["items"]:
            await q.message.reply_text("🛒 سبد خرید خالی است.")
            return
        bad = [i for i in s["items"] if i["stock"] < i["qty"]]
        if bad:
            await q.message.reply_text(
                "⚠️ موجودی این اقلام تغییر یافته است؛ سبد را بررسی کنید:\n"
                + "\n".join(f"• {i['name']} (موجود: {i['stock']})" for i in bad)
            )
            return
        c.user_data["await_loc"] = True
        await q.message.reply_text(
            "📍 جهت ثبت نهایی سفارش، موقعیت مکانی (Location) خود را ارسال فرمایید:",
            reply_markup=LOC_KB,
        )
    elif d.startswith("rev:"):
        pid = int(d[4:])
        avg, cnt = await db.rating(pid)
        com = await db.recent_comments(pid)
        lines = [f"⭐ امتیاز: <b>{avg}/5</b> ({cnt} نظر)"]
        for r in com:
            lines.append(f"\n{'⭐' * r['rating']}\n{esc(r['comment'])}")
        await q.message.reply_text(
            "\n".join(lines),
            parse_mode=HTML,
            reply_markup=IM([[IB("✍️ ثبت نظر و امتیاز", callback_data=f"rate:{pid}")]])
        )
    elif d.startswith("rate:"):
        pid = int(d[5:])
        await q.message.reply_text(
            "امتیاز خود را از ۱ تا ۵ ستاره مشخص نمایید:",
            reply_markup=IM(
                [
                    [
                        IB("⭐" * n, callback_data=f"rt:{pid}:{n}")
                        for n in (1, 2, 3, 4, 5)
                    ]
                ]
            ),
        )
    elif d.startswith("rt:"):
        _, pid, n = d.split(":")
        await db.add_rating(int(pid), uid, max(1, min(5, int(n))))
        c.user_data["awaiting"] = {"t": "comment", "pid": int(pid)}
        await show(
            q,
            f"✅ امتیاز {n} ستاره ثبت شد.\n\nدر صورت تمایل، نظر متنی خود را ارسال نمایید (یا «-» برای رد کردن):",
        )


# ====================== پنل مدیریت پیشرفته ======================
def admin_menu_kb(uid: int) -> IM:
    rows = [
        [IB("➕ افزودن محصول", callback_data="a:add"), IB("🔎 ویرایش / حذف تکی", callback_data="a:find")],
        [IB("📥 آپلود اکسل", callback_data="a:xls"), IB("📤 خروجی اکسل", callback_data="a:export")],
        [IB("🧾 سفارش‌ها", callback_data="a:orders"), IB("📊 آمار فروشگاه", callback_data="a:stats")],
        [IB("🎟 مدیریت کوپن‌ها", callback_data="a:coupons"), IB("📈 تخفیف پلکانی", callback_data="a:tiers")],
        [IB("🚚 هزینه ارسال", callback_data="a:ship"), IB("⚠️ آستانه موجودی کم", callback_data="a:low")],
        [IB("📢 پیام همگانی", callback_data="a:bc"), IB("📣 ارسال به کانال/گروه", callback_data="a:post")],
        [IB("📡 کانال‌ها و گروه‌ها", callback_data="a:ch"), IB("💾 دریافت پشتیبان دیتابیس", callback_data="a:backup")],
        [IB("🗑 پاکسازی دسته‌جمعی محصولات", callback_data="a:delprod_menu")],
    ]
    if db.is_owner(uid):
        rows.append([IB("👥 مدیریت ادمین‌ها", callback_data="a:admins")])
    return IM(rows)


async def cmd_admin(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not (db.is_admin(uid) or db.is_owner(uid)):
        await u.message.reply_text("⛔ دسترسی به پنل مدیریت امکان‌پذیر نیست.")
        return
    c.user_data.pop("flow", None)
    await u.message.reply_text(
        "👑 <b>پنل مدیریت پیشرفته ماهلا</b>\nگزینه مورد نظر خود را انتخاب فرمایید:",
        parse_mode=HTML,
        reply_markup=admin_menu_kb(uid),
    )


def admin_card(p: dict) -> tuple:
    has_photo = "دارد" if p.get("photo") else "ندارد"
    text = (
        f"⚙️ <b>مدیریت محصول</b>\n\n"
        f"🔖 کد: <code>{esc(p['code'])}</code>\n"
        f"🛍 نام: {esc(p['name'])}\n"
        f"🏷 برند: {esc(p['brand'] or '-')}\n"
        f"📂 دسته: {esc(p['category'])}\n"
        f"💰 قیمت: {money(p['price'])} تومان\n"
        f"📦 موجودی: {p['stock']}\n"
        f"🖼 تصویر: {has_photo}"
    )
    pid = p["id"]
    kb = IM(
        [
            [
                IB("نام", callback_data=f"a:ep:{pid}:name"),
                IB("برند", callback_data=f"a:ep:{pid}:brand"),
                IB("دسته", callback_data=f"a:ep:{pid}:category"),
            ],
            [
                IB("قیمت", callback_data=f"a:ep:{pid}:price"),
                IB("موجودی", callback_data=f"a:ep:{pid}:stock"),
                IB("کد", callback_data=f"a:ep:{pid}:code"),
            ],
            [
                IB("توضیحات", callback_data=f"a:ep:{pid}:description"),
                IB("🖼 عکس", callback_data=f"a:ep:{pid}:photo"),
            ],
            [IB("🗑 حذف این محصول", callback_data=f"a:dp:{pid}")],
            [IB("🔙 بازگشت به منو", callback_data="a:menu")],
        ]
    )
    return text, kb


FIELD_FA = {
    "name": "نام",
    "brand": "برند",
    "category": "دسته‌بندی",
    "price": "قیمت (تومان)",
    "stock": "موجودی",
    "code": "کد کالا",
    "description": "توضیحات",
    "photo": "تصویر (ارسال عکس یا لینک)",
}
BACK_MENU = IM([[IB("🔙 بازگشت به منوی مدیریت", callback_data="a:menu")]])


async def admin_cb(q, c, d: str, uid: int):
    if d == "menu":
        c.user_data.pop("flow", None)
        await show(q, "👑 <b>پنل مدیریت پیشرفته ماهلا</b>", admin_menu_kb(uid))

    elif d == "backup":
        await q.message.reply_text("⏳ در حال استخراج فایل پشتیبان از پایگاه داده...")
        bdata = await db.backup_bytes()
        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        bio = io.BytesIO(bdata)
        bio.name = f"mahla_backup_{now_str}.db"
        await q.message.reply_document(
            document=bio,
            caption=f"💾 <b>نسخه پشتیبان پایگاه داده</b>\n🕒 زمان استخراج: {now_str}",
            parse_mode=HTML,
        )

    elif d == "add":
        c.user_data["flow"] = {"t": "add", "i": 0, "data": {}}
        await q.message.reply_text(
            "➕ افزودن محصول جدید (انصراف با /cancel)\n\n" + ADD_STEPS[0][1]
        )
    elif d == "find":
        c.user_data["flow"] = {"t": "find"}
        await q.message.reply_text("🔎 نام یا کد محصول مورد نظر را ارسال فرمایید:")
    elif d.startswith("pm:"):
        p = await db.get_product(int(d[3:]))
        if p:
            text, kb = admin_card(p)
            await q.message.reply_text(text, parse_mode=HTML, reply_markup=kb)
    elif d.startswith("ep:"):
        _, pid, field = d.split(":")
        c.user_data["flow"] = {"t": "edit", "pid": int(pid), "field": field}
        await q.message.reply_text(
            f"✏️ مقدار جدید فیلد «{FIELD_FA.get(field, field)}» را ارسال فرمایید (انصراف: /cancel):"
        )
    elif d.startswith("dp:"):
        pid = d[3:]
        await show(
            q,
            "⚠️ آیا از حذف این کالا اطمینان دارید؟",
            IM(
                [
                    [
                        IB("✅ بله، حذف شود", callback_data=f"a:dpy:{pid}"),
                        IB("❌ خیر، انصراف", callback_data=f"a:pm:{pid}"),
                    ]
                ]
            ),
        )
    elif d.startswith("dpy:"):
        await db.delete_product(int(d[4:]))
        await show(q, "🗑 محصول مورد نظر حذف شد.", BACK_MENU)

    elif d == "delprod_menu":
        kb = IM(
            [
                [IB("🚨 حذف تمام محصولات (کل کاتالوگ)", callback_data="a:delall_pre")],
                [IB("📦 حذف محصولات ناموجود (موجودی صفر)", callback_data="a:delout_pre")],
                [IB("🔙 بازگشت به منو", callback_data="a:menu")],
            ]
        )
        await show(
            q,
            "⚠️ <b>بخش حذف دسته‌جمعی محصولات</b>\n\n"
            "هشدار: این عملیات حساس بوده و پیش از حذف کامل، نسخه پشتیبان پایگاه داده برای شما ارسال خواهد شد.",
            kb,
        )
    elif d == "delall_pre":
        await q.message.reply_text("⏳ در حال تهیه نسخه پشتیبان پیش از حذف کامل...")
        bdata = await db.backup_bytes()
        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        bio = io.BytesIO(bdata)
        bio.name = f"pre_purge_backup_{now_str}.db"
        await q.message.reply_document(
            document=bio,
            caption="💾 <b>نسخه پشتیبان اضطراری پیش از پاکسازی کل کاتالوگ</b>",
            parse_mode=HTML,
        )
        kb = IM(
            [
                [IB("🚨 تأیید نهایی: همه را پاک کن", callback_data="a:delall_confirm")],
                [IB("❌ انصراف و بازگشت", callback_data="a:delprod_menu")],
            ]
        )
        await q.message.reply_text(
            "⚠️ <b>تأیید مرحله دوم:</b>\nآیا از حذف <b>تمام محصولات کاتالوگ</b> اطمینان قطعی دارید؟ کلیه سبدهای فعال و نظرات نیز پاکسازی خواهند شد.",
            parse_mode=HTML,
            reply_markup=kb,
        )
    elif d == "delall_confirm":
        count = await db.delete_all_products()
        await show(q, f"🗑 عملیات پایان یافت. مجموعاً <b>{count}</b> محصول به طور کامل حذف شد.", BACK_MENU)
    elif d == "delout_pre":
        kb = IM(
            [
                [IB("✅ بله، ناموجودها پاک شوند", callback_data="a:delout_confirm")],
                [IB("❌ انصراف", callback_data="a:delprod_menu")],
            ]
        )
        await show(q, "⚠️ آیا از حذف کلیه محصولات ناموجود (موجودی صفر) اطمینان دارید؟", kb)
    elif d == "delout_confirm":
        count = await db.delete_out_of_stock()
        await show(q, f"🗑 عملیات انجام شد. تعداد <b>{count}</b> محصول ناموجود پاکسازی گردید.", BACK_MENU)

    elif d == "admins":
        if not db.is_owner(uid):
            await show(q, "⛔ تنها مالک اصلی سامانه به این بخش دسترسی دارد.", BACK_MENU)
            return
        admins_list = await db.list_admins()
        lines = ["👥 <b>مدیریت ادمین‌های ربات</b>\n"]
        rows = []
        for adm in admins_list:
            aid = adm["user_id"]
            role_badge = "👑 مالک اصلی" if adm["role"] == "owner" else "👤 ادمین"
            name_label = f" ({esc(adm['name'])})" if adm.get("name") else ""
            lines.append(f"• <code>{aid}</code> — {role_badge}{name_label}")
            if adm["role"] != "owner":
                rows.append([IB(f"❌ حذف ادمین {aid}", callback_data=f"a:admrem:{aid}")])
        rows.append([IB("➕ افزودن ادمین جدید با آیدی", callback_data="a:admadd")])
        rows.append([IB("🔙 بازگشت به منو", callback_data="a:menu")])
        await show(q, "\n".join(lines), IM(rows))
    elif d == "admadd":
        if not db.is_owner(uid):
            return
        c.user_data["flow"] = {"t": "admadd"}
        await q.message.reply_text(
            "👤 لطفاً <b>شناسه عددی (Numeric Telegram User ID)</b> فرد مورد نظر را ارسال فرمایید:\n(می‌توانید شناسه را با یک فاصله همراه با نام دلخواه بفرستید، مانند: <code>123456789 علی</code>)",
            parse_mode=HTML,
        )
    elif d.startswith("admrem:"):
        if not db.is_owner(uid):
            return
        target_uid = int(d.split(":")[1])
        res = await db.remove_admin(target_uid)
        if res == "removed":
            await show(q, f"✅ ادمین با شناسه <code>{target_uid}</code> حذف شد.", BACK_MENU)
        else:
            await show(q, "❌ حذف ادمین مقدور نیست.", BACK_MENU)

    elif d == "xls":
        await q.message.reply_text(
            "📥 <b>آپلود هوشمند اکسل</b>\n\n"
            "فایل اکسل (xlsx / xls) یا CSV را ارسال فرمایید تا به صورت خودکار تحلیل و ذخیره گردد:\n"
            "• هر شکل جدول، با هر ترتیب ستون، با عناوین فارسی یا انگلیسی پذیرفته می‌شود.\n"
            "• اگر در کپشن فایل عبارت «جایگزین» را بنویسید، اقلامی که در فایل نیستند از دیتابیس پاک خواهند شد.\n"
            "• ستون‌های فرعی به عنوان مشخصات تکمیلی کالا ثبت می‌شوند.",
            parse_mode=HTML,
        )
    elif d == "export":
        prods = await db.all_products()
        if not prods:
            await q.message.reply_text("محصولی در فروشگاه ثبت نشده است.")
            return
        data = await asyncio.to_thread(export_xlsx, prods)
        await q.message.reply_document(
            document=data,
            filename="mahla-products.xlsx",
            caption=f"📤 خروجی کاتالوگ فروشگاه شامل {len(prods)} محصول",
        )

    elif d == "orders":
        orders = await db.admin_orders()
        if not orders:
            await show(q, "سفارشی ثبت نشده است.", BACK_MENU)
            return
        rows = [
            [
                IB(
                    f"{STATUS_ICON.get(o['status'], '')} #{o['id']} • {money(o['total'])} • {o['status']}",
                    callback_data=f"a:o:{o['id']}",
                )
            ]
            for o in orders
        ]
        rows.append([IB("🔙 بازگشت به منو", callback_data="a:menu")])
        await show(q, "🧾 <b>مدیریت سفارش‌ها</b> (سفارش‌های فعال در صدر):", IM(rows))
    elif d.startswith("o:"):
        o = await db.get_order(int(d[2:]))
        if o:
            await show(q, order_text(o, True), status_kb(o))
            if o["lat"] is not None:
                await c.bot.send_location(
                    uid, latitude=o["lat"], longitude=o["lon"]
                )
    elif d.startswith("st:"):
        _, oid, i = d.split(":")
        o = await db.set_order_status(int(oid), STATUSES[int(i)])
        if not o:
            return
        if not o["changed"]:
            await q.message.reply_text("ℹ️ وضعیت تغییر نیافت (سفارش لغوشده بازگردانی نمی‌شود).")
            return
        await show(q, order_text(o, True), status_kb(o))
        try:
            await c.bot.send_message(
                o["user_id"],
                f"🔔 وضعیت سفارش #{o['id']} شما به‌روزرسانی شد:\n{STATUS_ICON[o['status']]} <b>{esc(o['status'])}</b>",
                parse_mode=HTML,
            )
        except TelegramError:
            await q.message.reply_text("⚠️ ارسال اعلان به مشتری مقدور نبود (احتمال مسدودی ربات).")

    elif d == "stats":
        s = await db.stats()
        text = (
            f"📊 <b>آمار جامع فروشگاه ماهلا</b>\n\n"
            f"📦 کل محصولات: <b>{s['products']}</b> (ناموجود: {s['out']} | کم‌موجودی: {s['low']})\n"
            f"👥 کاربران ثبت‌شده: <b>{s['users']}</b>\n"
            f"🧾 سفارش‌ها: <b>{s['orders']}</b> (در انتظار بررسی: <b>{s['pending']}</b>)\n"
            f"💰 درآمد کل (بدون لغوشده‌ها): <b>{money(s['revenue'])} تومان</b>\n\n"
            f"📅 سفارش‌های امروز: <b>{s['today_orders']}</b> | فروش امروز: <b>{money(s['today_revenue'])} تومان</b>\n"
            f"🎟 کدهای تخفیف فعال: <b>{s['coupons']}</b> | تعداد مدیران: <b>{s['admins']}</b>"
        )
        await show(q, text, BACK_MENU)

    elif d == "coupons":
        cps = await db.list_coupons()
        lines = ["🎟 <b>مدیریت کدهای تخفیف</b>\n"]
        rows = []
        for x in cps:
            st_icon = "🟢" if x["active"] else "⏸"
            if x.get("expired"):
                st_icon = "⌛"
            elif x.get("full"):
                st_icon = "🚫"
            lbl = db.coupon_label(x)
            lines.append(
                f"{st_icon} <code>{esc(x['code'])}</code> ({lbl}) — استفاده: {x['used']}/{x['max_uses'] or '∞'}"
            )
            tog_btn = "⏸ غیرفعال" if x["active"] else "▶️ فعال‌سازی"
            rows.append(
                [
                    IB(tog_btn, callback_data=f"a:cptog:{x['code']}"),
                    IB(f"🗑 {x['code']}", callback_data=f"a:cpdel:{x['code']}"),
                ]
            )
        rows.append([IB("➕ افزودن کوپن جدید", callback_data="a:cpadd")])
        rows.append([IB("🧹 حذف کدهای منقضی/تکمیل", callback_data="a:cpdel_exp")])
        rows.append([IB("🚨 حذف تمام کوپن‌ها", callback_data="a:cpdel_all")])
        rows.append([IB("🔙 بازگشت به منو", callback_data="a:menu")])
        await show(q, "\n".join(lines) if cps else "🎟 هنوز کدی ثبت نشده است.", IM(rows))
    elif d.startswith("cptog:"):
        code = d[6:]
        st = await db.toggle_coupon(code)
        await show(
            q,
            f"وضعیت کوپن {code} به {'فعال' if st else 'غیرفعال'} تغییر یافت.",
            BACK_MENU,
        )
    elif d.startswith("cpdel:"):
        await db.del_coupon(d[6:])
        await show(q, "🗑 کوپن با موفقیت حذف شد.", BACK_MENU)
    elif d == "cpdel_exp":
        n = await db.del_expired_coupons()
        await show(q, f"🧹 تعداد {n} کوپن منقضی یا تکمیل‌شده پاکسازی گردید.", BACK_MENU)
    elif d == "cpdel_all":
        n = await db.del_all_coupons()
        await show(q, f"🗑 تمام کوپن‌ها ({n} عدد) حذف شدند.", BACK_MENU)
    elif d == "cpadd":
        c.user_data["flow"] = {"t": "coupon"}
        await q.message.reply_text(
            "🎟 <b>افزودن کد تخفیف جدید</b>\n\n"
            "فرمت ساده: <code>کد|درصد|سقف_تعداد</code>\nمثال: <code>MAHLA10|10|50</code>\n\n"
            "فرمت پیشرفته: <code>کد|درصد|مبلغ_ثابت|حداقل_خرید|سقف_تعداد|تاریخ_انقضا(YYYY-MM-DD)|تک‌بار_مصرف(0یا1)</code>\n"
            "مثال: <code>NOORUZ|0|50000|200000|100|2026-12-29|1</code>",
            parse_mode=HTML,
        )

    elif d == "tiers":
        ts = await db.list_tiers()
        lines = ["📈 <b>تخفیف‌های پلکانی سبد خرید</b>\n"] + [
            f"خرید بالای {money(x['min_amount'])} تومان → {x['percent']}٪ تخفیف"
            for x in ts
        ]
        rows = [
            [
                IB(
                    f"🗑 حذف پله {money(x['min_amount'])}",
                    callback_data=f"a:tdel:{x['min_amount']}",
                )
            ]
            for x in ts
        ]
        rows += [
            [IB("➕ پله جدید", callback_data="a:tadd")],
            [IB("🔙 بازگشت به منو", callback_data="a:menu")],
        ]
        await show(q, "\n".join(lines) if ts else "📈 پله‌ای تعریف نشده است.", IM(rows))
    elif d == "tadd":
        c.user_data["flow"] = {"t": "tier"}
        await q.message.reply_text(
            "📈 فرمت: <code>مبلغ|درصد</code>\nمثال: <code>500000|5</code> (خرید بالای ۵۰۰ هزار تومان شامل ۵٪ تخفیف)",
            parse_mode=HTML,
        )
    elif d.startswith("tdel:"):
        await db.del_tier(int(d[5:]))
        await show(q, "🗑 پله تخفیف حذف شد.", BACK_MENU)

    elif d == "ship":
        c.user_data["flow"] = {"t": "ship"}
        cur = await db.get_setting("shipping_fee", "0")
        await q.message.reply_text(
            f"🚚 هزینه ارسال فعلی: {money(cur)} تومان\nمبلغ جدید را ارسال فرمایید (۰ برای ارسال رایگان):"
        )
    elif d == "low":
        c.user_data["flow"] = {"t": "low"}
        cur = await db.get_setting("low_stock", "5")
        await q.message.reply_text(
            f"⚠️ آستانه هشدار موجودی کم (فعلی: {cur} عدد)\nعدد جدید را ارسال فرمایید:"
        )
    elif d in ("bc", "post"):
        chans = json.loads(await db.get_setting("channels", "[]"))
        if d == "post" and not chans:
            await q.message.reply_text("ابتدا از بخش «کانال‌ها و گروه‌ها» یک مقصد تعریف فرمایید.")
            return
        c.user_data["flow"] = {"t": d}
        dest = "کلیه کاربران بات" if d == "bc" else "کانال‌ها و گروه‌های ثبت‌شده"
        await q.message.reply_text(f"📢 محتوای پیام (متن، عکس، ویدیو و ...) را جهت ارسال به {dest} بفرستید:")
    elif d in ("bcok", "bcno"):
        pend = c.user_data.pop("pending_send", None)
        if d == "bcno" or not pend:
            await show(q, "❌ ارسال پیام لغو شد.")
            return
        await show(q, "⏳ فرآیند ارسال آغاز گردید؛ پس از پایان گزارش ارسال خواهد شد.")
        c.application.create_task(do_send(c.bot, pend))
    elif d == "ch":
        chans = json.loads(await db.get_setting("channels", "[]"))
        lines = ["📡 <b>کانال‌ها و گروه‌های متصل</b>\n"] + [f"• {esc(x['title'])}" for x in chans]
        rows = [
            [IB(f"🗑 {x['title'][:25]}", callback_data=f"a:chdel:{x['id']}")]
            for x in chans
        ]
        rows += [
            [IB("➕ افزودن مقصد جدید", callback_data="a:chadd")],
            [IB("🔙 بازگشت به منو", callback_data="a:menu")],
        ]
        await show(q, "\n".join(lines), IM(rows))
    elif d == "chadd":
        c.user_data["flow"] = {"t": "chadd"}
        await q.message.reply_text(
            "آیدی عددی یا یوزرنیم کانال/گروه را ارسال فرمایید (ابتدا ربات را ادمین کنید):\nمانند: <code>@channel_id</code> یا <code>-1001234567890</code>",
            parse_mode=HTML,
        )
    elif d.startswith("chdel:"):
        chans = [
            x
            for x in json.loads(await db.get_setting("channels", "[]"))
            if str(x["id"]) != d[6:]
        ]
        await db.set_setting("channels", json.dumps(chans, ensure_ascii=False))
        await show(q, "🗑 کانال/گروه با موفقیت حذف گردید.", BACK_MENU)


# ====================== جریان تعاملی ورودی‌های ادمین ======================
ADD_STEPS = [
    ("code", "🔖 کد محصول را ارسال فرمایید (یا «-» برای تخصیص کد خودکار):"),
    ("name", "🛍 نام محصول:"),
    ("brand", "🏷 نام برند (یا «-»):"),
    ("category", "📂 نام دسته‌بندی:"),
    ("price", "💰 قیمت به تومان:"),
    ("stock", "📦 موجودی اولیه انبار:"),
    ("description", "📝 توضیحات محصول (یا «-»):"),
    ("photo", "🖼 عکس کالا را بفرستید (یا «-» جهت رد کردن):"),
]


def _photo_value(m, txt: str):
    if m.photo:
        return m.photo[-1].file_id
    if txt.lower().startswith("http"):
        return txt
    return None


async def owner_flow(u: Update, c: ContextTypes.DEFAULT_TYPE, flow: dict):
    m = u.message
    txt = (m.text or m.caption or "").strip()
    t = flow["t"]

    if t == "add":
        key = ADD_STEPS[flow["i"]][0]
        val = None
        if key == "photo":
            val = _photo_value(m, txt)
            if val is None and txt != "-":
                await m.reply_text("تصویر ارسال نمایید، لینک معتبر بفرستید یا «-» بزنید.")
                return
        elif not txt:
            await m.reply_text("ورودی نامعتبر است.")
            return
        elif key in ("price", "stock"):
            val = to_int(txt)
            if val is None or val < 0:
                await m.reply_text("لطفاً عدد صحیح و نامنفی بفرستید.")
                return
        elif key == "code":
            val = None if txt == "-" else ncode(txt)
        elif key == "name":
            if txt == "-":
                await m.reply_text("ثبت نام الزامی است.")
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
        status_txt = "✅ کالا افزوده شد." if res == "added" else "✅ کالای هم‌کد به‌روزرسانی شد."
        await m.reply_text(f"{status_txt}\n\n{text}", parse_mode=HTML, reply_markup=kb)

    elif t == "edit":
        field = flow["field"]
        if field == "photo":
            val = _photo_value(m, txt)
            if val is None:
                await m.reply_text("تصویر یا لینک معتبر بفرستید.")
                return
        elif field in ("price", "stock"):
            val = to_int(txt)
            if val is None or val < 0:
                await m.reply_text("لطفاً منحصراً مقدار عددی معتبر بفرستید.")
                return
        elif not txt:
            await m.reply_text("ورودی متنی لازم است.")
            return
        else:
            val = ncode(txt) if field == "code" else txt

        r = await db.update_field(flow["pid"], field, val)
        if r == "dup":
            await m.reply_text("❌ این کد کالا تکراری است. کد دیگری بفرستید:")
            return
        c.user_data.pop("flow", None)
        p = await db.get_product(flow["pid"])
        text, kb = admin_card(p)
        await m.reply_text(f"✅ ویرایش ذخیره گردید.\n\n{text}", parse_mode=HTML, reply_markup=kb)

    elif t == "find":
        c.user_data.pop("flow", None)
        res = await db.search(txt) if txt else []
        if not res:
            await m.reply_text("❌ کالا یافت نشد.", reply_markup=BACK_MENU)
        elif len(res) == 1:
            text, kb = admin_card(res[0])
            await m.reply_text(text, parse_mode=HTML, reply_markup=kb)
        else:
            await m.reply_text(
                "کالای مورد نظر را تعیین فرمایید:",
                reply_markup=IM(
                    [
                        [
                            IB(
                                f"{p['name'][:30]} ({p['code']})",
                                callback_data=f"a:pm:{p['id']}",
                            )
                        ]
                        for p in res[:12]
                    ]
                ),
            )

    elif t == "admadd":
        parts = txt.split(maxsplit=1)
        raw_id = to_int(parts[0])
        name = parts[1] if len(parts) > 1 else ""
        if not raw_id:
            await m.reply_text("❌ شناسه عددی نامعتبر است.")
            return
        c.user_data.pop("flow", None)
        status = await db.add_admin(raw_id, name)
        if status == "owner":
            await m.reply_text("ℹ️ این شناسه متعلق به مالک اصلی سامانه است.", reply_markup=BACK_MENU)
        elif status == "exists":
            await m.reply_text(f"ℹ️ شناسه {raw_id} از قبل ادمین بود و مشخصات آن به‌روز شد.", reply_markup=BACK_MENU)
        else:
            await m.reply_text(f"✅ کاربر {raw_id} به فهرست ادمین‌ها افزوده گردید.", reply_markup=BACK_MENU)

    elif t == "coupon":
        parts = [x.strip() for x in re.split(r"[|\s,،]+", txt) if x.strip()]
        if len(parts) < 2:
            await m.reply_text("❌ حداقل وارد کردن کد و مقدار تخفیف الزامی است.")
            return
        code = ncode(parts[0])
        try:
            if len(parts) <= 3:
                pct = int(to_int(parts[1], 0))
                mx = int(to_int(parts[2], 0)) if len(parts) > 2 else 0
                await db.add_coupon(code=code, percent=pct, max_uses=mx)
            else:
                pct = int(to_int(parts[1], 0))
                fixed = int(to_int(parts[2], 0))
                min_amt = int(to_int(parts[3], 0))
                mx = int(to_int(parts[4], 0)) if len(parts) > 4 else 0
                exp = parts[5] if len(parts) > 5 and parts[5] != "-" else None
                one_per = int(to_int(parts[6], 0)) if len(parts) > 6 else 0
                await db.add_coupon(
                    code=code,
                    percent=pct,
                    fixed=fixed,
                    min_amount=min_amt,
                    max_uses=mx,
                    expires_at=exp,
                    one_per_user=one_per,
                )
            c.user_data.pop("flow", None)
            await m.reply_text(f"✅ کوپن تخفیف {code} ذخیره شد.", reply_markup=BACK_MENU)
        except Exception as e:
            await m.reply_text(f"❌ خطا در پردازش اطلاعات کوپن: {e}")

    elif t == "tier":
        parts = [x for x in re.split(r"[|\s,،]+", txt) if x]
        try:
            amt, pct = to_int(parts[0]), to_int(parts[1])
            assert amt and amt > 0 and pct and 1 <= pct <= 100
            c.user_data.pop("flow", None)
            await db.add_tier(amt, pct)
            await m.reply_text(f"✅ تخفیف پلکانی بالای {money(amt)} تومان با {pct}٪ ذخیره شد.", reply_markup=BACK_MENU)
        except Exception:
            await m.reply_text("❌ فرمت نامعتبر: مبلغ|درصد (مثال: 500000|5)")

    elif t in ("ship", "low"):
        v = to_int(txt)
        if v is None or v < 0:
            await m.reply_text("لطفاً عدد صحیح و نامنفی بفرستید.")
            return
        c.user_data.pop("flow", None)
        await db.set_setting("shipping_fee" if t == "ship" else "low_stock", v)
        await m.reply_text("✅ مقدار جدید با موفقیت تنظیم شد.", reply_markup=BACK_MENU)

    elif t == "chadd":
        ref = txt if txt.lstrip("-").isdigit() is False else int(txt)
        try:
            chat = await c.bot.get_chat(ref)
        except TelegramError:
            await m.reply_text("❌ مقصد یافت نشد؛ اطمینان حاصل فرمایید ربات در آن ادمین است.")
            return
        chans = json.loads(await db.get_setting("channels", "[]"))
        if all(x["id"] != chat.id for x in chans):
            chans.append({"id": chat.id, "title": chat.title or chat.username or str(chat.id)})
            await db.set_setting("channels", json.dumps(chans, ensure_ascii=False))
        c.user_data.pop("flow", None)
        await m.reply_text(f"✅ «{chat.title or chat.username}» ثبت گردید.", reply_markup=BACK_MENU)

    elif t in ("bc", "post"):
        c.user_data.pop("flow", None)
        targets = (
            await db.user_ids()
            if t == "bc"
            else [x["id"] for x in json.loads(await db.get_setting("channels", "[]"))]
        )
        c.user_data["pending_send"] = {
            "chat": m.chat_id,
            "msg": m.message_id,
            "targets": targets,
            "kind": t,
        }
        await m.reply_text(
            f"آیا این پیام برای {len(targets)} مقصد ارسال گردد؟",
            reply_markup=IM(
                [
                    [
                        IB("✅ بله، ارسال کن", callback_data="a:bcok"),
                        IB("❌ لغو", callback_data="a:bcno"),
                    ]
                ]
            ),
        )


async def do_send(bot, pend: dict):
    sent = failed = 0
    for tid in pend["targets"]:
        for _ in range(2):
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
    for aid in db.admin_ids_sync():
        try:
            await bot.send_message(
                aid, f"📢 ارسال گروهی پایان یافت.\n✅ موفق: {sent}\n❌ ناموفق: {failed}"
            )
        except TelegramError:
            pass


async def owner_photo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    m = u.message
    code = (m.caption or "").strip()
    if not code:
        return
    n = await db.set_photo(code, m.photo[-1].file_id)
    await m.reply_text(
        f"🖼 تصویر محصول با کد {ncode(code)} به‌روزرسانی شد."
        if n
        else f"❌ محصولی با کد {ncode(code)} یافت نشد."
    )


async def import_excel(u: Update, c: ContextTypes.DEFAULT_TYPE):
    m = u.message
    doc = m.document
    name = doc.file_name or "file"
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext not in ("xlsx", "xlsm", "xls", "csv", "tsv", "txt"):
        await m.reply_text("تنها ارسال فایل اکسل یا CSV مجاز است.")
        return
    status_msg = await m.reply_text("⏳ در حال خواندن و اعتبارسنجی فایل...")
    try:
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        res = await asyncio.to_thread(parse_file, data, name)
    except Exception as e:
        log.exception("خطا در پردازش اکسل")
        await status_msg.edit_text(f"❌ پردازش فایل با خطا مواجه شد: {e}")
        return

    if not res["products"]:
        lines = ["❌ کالایی شناسایی نشد."] + res["notes"]
        await status_msg.edit_text("\n".join(lines))
        return

    replace_mode = "جایگزین" in (m.caption or "")
    r = await db.bulk_upsert(res["products"], replace=replace_mode)
    lines = [
        f"✅ <b>{len(res['products'])} محصول پردازش گردید</b>",
        f"🆕 کالاهای جدید: {r['added']} | 🔄 به‌روزرسانی‌شده: {r['updated']}",
    ]
    if replace_mode:
        lines.append(f"🗑 اقلام حذف‌شده (غایب در فایل): {r['removed']}")
    lines.append("\n🧭 ستون‌های شناسایی‌شده:\n" + "\n".join(esc(x) for x in res["mapping"]))
    if res["stock_missing"]:
        lines.append(
            f"⚠️ ستون موجودی یافت نشد؛ پیش‌فرض انبار ({db.DEFAULT_STOCK}) اعمال گردید."
        )
    lines += [esc(x) for x in res["notes"]]
    await status_msg.edit_text("\n".join(lines)[:4000], parse_mode=HTML)


# ====================== مدیریت خطاها و نقطه ورود برنامه ======================
async def on_error(u: object, c: ContextTypes.DEFAULT_TYPE):
    err = c.error
    if isinstance(err, NetworkError):
        log.warning("خطای شبکه گذرا: %s", err)
        return
    log.error("خطای پیش‌بینی‌نشده در اجرای ربات", exc_info=err)
    try:
        if isinstance(u, Update) and u.effective_message:
            await u.effective_message.reply_text("⚠️ خطایی رخ داد؛ لطفاً دوباره تلاش نمایید.")
    except TelegramError:
        pass


def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ متغیر محیطی BOT_TOKEN یافت نشد!")

    db.init_sync(owner_id=OWNER_ID)
    log.info("پایگاه داده متصل شد: %s (مالک: %s)", db.DB_PATH, OWNER_ID)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(15)
        .read_timeout(25)
        .write_timeout(25)
        .pool_timeout(15)
        .get_updates_connect_timeout(15)
        .get_updates_read_timeout(35)
        .get_updates_pool_timeout(15)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    log.info("✅ ربات ماهلا با موفقیت راه‌اندازی شد.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
