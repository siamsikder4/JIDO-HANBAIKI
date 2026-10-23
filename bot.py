# -*- coding: utf-8 -*-
"""
Your Proxy Shop Bot  (Firebase edition, v2)
-------------------------------------------
* Bottom (reply-keyboard) menu for users  +  full Admin Panel as bottom menu for admins
* Admin can change COLOR and CUSTOM EMOJI of every button (user side + admin side) from the panel
* Categories / products / stock / deposits / users / broadcast / notice / settings - all via buttons
* Pretty profile card, order invoice UI, paginated user list
* Firebase Realtime DB (falls back to local data.json)

Requires: aiogram >= 3.25, firebase-admin, python-dotenv
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

load_dotenv()

# ═════════════ CONFIG ═════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
BRAND = os.getenv("BRAND_NAME", "Your Proxy")
CURRENCY = os.getenv("CURRENCY", "BDT")
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "").strip()
FIREBASE_CRED = os.getenv("FIREBASE_CRED", "firebase.json")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "").strip()
STORE_FILE = os.getenv("STORE_FILE", "data.json")

# Every button: key -> (label, default color, fallback emoji, group)
# color: "primary" (blue) | "success" (green) | "danger" (red) | None (default)
BTN = {
    # user - bottom menu
    "buy": ("Buy Product", "success", "🛒", "user"),
    "my_profile": ("My Profile", "primary", "👤", "user"),
    "deposit": ("Deposit", "primary", "💰", "user"),
    "code": ("Get Code", "success", "🔑", "user"),
    "support": ("Support", "primary", "💬", "user"),
    "admin": ("Admin Panel", "danger", "👑", "user"),
    # user - inline
    "cancel": ("Cancel", "danger", "❌", "user"),
    "back": ("Back / Back to Categories", "danger", "⬅️", "user"),
    "cat": ("Category buttons", "primary", "📁", "user"),
    "item": ("Product buttons", "success", "📦", "user"),
    "buynow": ("Buy Now", "success", "🛒", "user"),
    "confirm": ("Confirm", "success", "✅", "user"),
    "my_orders": ("My Orders", "primary", "📦", "user"),
    "order_btn": ("Order list buttons", None, "🧾", "user"),
    "contact": ("Contact Admin", "primary", "💬", "user"),
    "pay": ("Payment methods", "primary", "💳", "user"),
    "join": ("Join / I've Joined", "success", "✅", "user"),
    # admin - bottom menu
    "a_dash": ("Dashboard", "primary", "📊", "admin"),
    "a_users": ("Users", "primary", "👥", "admin"),
    "a_prod": ("Products", "success", "🗃", "admin"),
    "a_cat": ("Categories", "primary", "📁", "admin"),
    "a_deps": ("Deposits", "success", "💰", "admin"),
    "a_bc": ("Broadcast", "primary", "📣", "admin"),
    "a_notice": ("Notice", None, "📢", "admin"),
    "a_set": ("Settings", None, "⚙️", "admin"),
    "a_style": ("Button Colors", "primary", "🎨", "admin"),
    "a_emoji": ("Custom Emoji", "primary", "⭐", "admin"),
    "a_exit": ("User Menu", "danger", "🏠", "admin"),
    # admin - inline
    "x_ok": ("Approve / Save / Send", "success", "✅", "admin"),
    "x_no": ("Reject / Cancel / Delete", "danger", "❌", "admin"),
    "x_add": ("Add / Create", "success", "➕", "admin"),
    "x_edit": ("Edit / Action", "primary", "✏️", "admin"),
    "x_item": ("List item buttons", None, "▫️", "admin"),
    "x_nav": ("Back / Navigation", None, "⬅️", "admin"),
}
USER_KEYS = ["buy", "my_profile", "deposit", "code", "support", "admin"]
ADMIN_KEYS = ["a_dash", "a_users", "a_prod", "a_cat", "a_deps", "a_bc", "a_notice", "a_set",
              "a_style", "a_emoji", "a_exit"]

# Emojis used inside message text (custom emoji can be set for each from admin panel)
EMOJI = {
    "fire": "🔥", "ok": "✅", "no": "❌", "wallet": "💳", "profile": "👤", "lock": "🔒",
    "notice": "📢", "stock": "🗃", "orders": "📦", "cart": "🛒", "bolt": "⚡", "party": "🎉",
    "stats": "📊", "users": "👥", "ban": "🚫", "settings": "⚙️", "invoice": "🧾",
    "money": "💵", "date": "📅", "crown": "👑", "broadcast": "📣", "star": "⭐", "down": "⬇️",
}
STYLE_LABEL = {"primary": "🔵 Blue", "success": "🟢 Green", "danger": "🔴 Red", "none": "⚪ Default"}
STYLE_MARK = {"primary": "🔵", "success": "🟢", "danger": "🔴", None: "⚪"}

DEFAULT_PAYMENTS = (
    "bKash|01XXXXXXXXX (Send Money)\n"
    "Nagad|01XXXXXXXXX (Send Money)\n"
    "Binance Pay|XXXXXXXX"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("shop")

# ═════════════ STORAGE (memory cache + Firebase write-through) ═════════════
D: dict[str, dict] = {k: {} for k in ("users", "cats", "products", "stock", "orders", "deposits", "settings", "seq")}
SET = D["settings"]
fb_ref = None
store_lock = asyncio.Lock()
buy_lock = asyncio.Lock()
ADMIN_MODE: set[int] = set()     # admins currently showing the admin bottom menu


def _norm(v) -> dict:
    if isinstance(v, list):
        return {str(i): x for i, x in enumerate(v) if x is not None}
    if isinstance(v, dict):
        return {str(k): x for k, x in v.items()}
    return {}


def init_store():
    global fb_ref
    data = {}
    if FIREBASE_DB_URL:
        import firebase_admin
        from firebase_admin import credentials, db as fdb
        firebase_admin.initialize_app(credentials.Certificate(FIREBASE_CRED), {"databaseURL": FIREBASE_DB_URL})
        fb_ref = fdb.reference("/")
        data = fb_ref.get() or {}
        log.info("Firebase connected")
    else:
        log.warning("FIREBASE_DB_URL not set -> using local file %s", STORE_FILE)
        if os.path.exists(STORE_FILE):
            with open(STORE_FILE, encoding="utf-8") as f:
                data = json.load(f)
    for coll in D:
        D[coll].clear()
        D[coll].update(_norm(data.get(coll)))


def _persist(coll: str, keys: tuple):
    if fb_ref:
        fb_ref.child(coll).update({k: D[coll].get(k) for k in keys})
    else:
        with open(STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(D, f, ensure_ascii=False)


async def put(coll: str, *keys):
    if not keys:
        return
    async with store_lock:
        await asyncio.to_thread(_persist, coll, tuple(str(k) for k in keys))


async def nid(name: str) -> int:
    D["seq"][name] = int(D["seq"].get(name, 0)) + 1
    await put("seq", name)
    return D["seq"][name]


async def set_setting(key: str, value: str):
    SET[key] = value
    await put("settings", key)


async def del_setting(key: str):
    SET.pop(key, None)
    await put("settings", key)


async def bump_menu():
    """Force every user's bottom keyboard to refresh on their next message."""
    await set_setting("mv", str(int(time.time() * 1000)))


def get_user(uid):
    return D["users"].get(str(uid))


def prod(pid):
    return D["products"].get(str(pid))


def stk_left(pid) -> int:
    return sum(1 for s in D["stock"].values() if int(s["pid"]) == int(pid) and not s["sold"])


def product_row(pid):
    p = prod(pid)
    return {**p, "stk": stk_left(pid)} if p else None


def cat_name(cid) -> str:
    c = D["cats"].get(str(cid))
    return c["name"] if c else "?"


async def touch_user(u):
    row = get_user(u.id)
    name, un = u.full_name or "", u.username or ""
    if row is None:
        row = {"id": u.id, "username": un, "full_name": name, "balance": 0.0,
               "spent": 0.0, "banned": 0, "joined_at": int(time.time())}
        D["users"][str(u.id)] = row
        await put("users", u.id)
    elif row.get("username") != un or row.get("full_name") != name:
        row["username"], row["full_name"] = un, name
        await put("users", u.id)
    return row


async def seed_cats():
    if D["cats"]:
        return
    for name, fb in (("VPN", "🛡"), ("Proxy", "🌐"), ("Mail", "✉️")):
        cid = await nid("cats")
        D["cats"][str(cid)] = {"id": cid, "name": name, "emoji": "", "fb": fb}
    await put("cats", *D["cats"].keys())
    low = {c["name"].lower(): c["id"] for c in D["cats"].values()}
    for p in D["products"].values():            # migrate old string categories
        if isinstance(p.get("category"), str) and p["category"].lower() in low:
            p["category"] = low[p["category"].lower()]
    await put("products", *D["products"].keys())


# ═════════════ HELPERS / UI ═════════════
esc = html.escape
BAR = "━━━━━━━━━━━━━━━"


def money(x) -> str:
    x = float(x)
    s = f"{x:,.0f}" if x == int(x) else f"{x:,.2f}"
    return f"{s} {CURRENCY}"


def fdate(ts) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%d %b %Y, %H:%M")


def emoji_id(key: str) -> str:
    return SET.get(f"emoji:{key}", "") or ""


def E(key: str) -> str:
    ch = EMOJI.get(key) or (BTN[key][2] if key in BTN else "•")
    cid = emoji_id(key)
    return f'<tg-emoji emoji-id="{cid}">{ch}</tg-emoji>' if cid else ch


def bstyle(key):
    v = SET.get(f"style:{key}")
    if v is None:
        return BTN[key][1] if key in BTN else None
    return None if v == "none" else v


def B(text, cb=None, key=None, url=None, cid=None, fb=None, st=None) -> InlineKeyboardButton:
    """Inline button. Color + custom emoji come from the admin-controlled registry `key`."""
    kw = {}
    style = st if st else (bstyle(key) if key else None)
    if style:
        kw["style"] = style
    cid = cid or (emoji_id(key) if key else "")
    label = text
    if cid:
        kw["icon_custom_emoji_id"] = cid
    else:
        ch = fb if fb is not None else (BTN[key][2] if key in BTN else "")
        if ch:
            label = f"{ch} {text}"
    if url:
        return InlineKeyboardButton(text=label, url=url, **kw)
    return InlineKeyboardButton(text=label, callback_data=cb, **kw)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def RB(key: str) -> KeyboardButton:
    name, _, ch, _g = BTN[key]
    kw, label = {}, name
    st = bstyle(key)
    if st:
        kw["style"] = st
    cid = emoji_id(key)
    if cid:
        kw["icon_custom_emoji_id"] = cid
    else:
        label = f"{ch} {name}"
    return KeyboardButton(text=label, **kw)


def user_menu_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [[RB("buy")], [RB("my_profile"), RB("deposit")], [RB("code"), RB("support")]]
    if uid in ADMIN_IDS:
        rows.append([RB("admin")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Choose an option 👇")


def admin_menu_kb() -> ReplyKeyboardMarkup:
    rows = [[RB("a_dash"), RB("a_users")], [RB("a_prod"), RB("a_cat")], [RB("a_deps"), RB("a_bc")],
            [RB("a_notice"), RB("a_set")], [RB("a_style"), RB("a_emoji")], [RB("a_exit")]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Admin Panel 👑")


def menu_kb(uid: int) -> ReplyKeyboardMarkup:
    return admin_menu_kb() if (uid in ADMIN_IDS and uid in ADMIN_MODE) else user_menu_kb(uid)


NAME2KEY = {BTN[k][0].lower(): k for k in USER_KEYS + ADMIN_KEYS}


def menu_key(text):
    t = re.sub(r"^[\W_]+", "", (text or "").strip()).lower()
    return NAME2KEY.get(t)


def paginate(items, page, per):
    pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def nav_row(prefix, page, pages):
    if pages <= 1:
        return []
    r = []
    if page > 0:
        r.append(B("Prev", f"{prefix}:{page - 1}", "x_nav", fb="◀️"))
    r.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        r.append(B("Next", f"{prefix}:{page + 1}", "x_nav", fb="▶️"))
    return r


async def show(ev, text, markup=None):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await ev.message.answer(text, reply_markup=markup)
        try:
            await ev.answer()
        except Exception:
            pass
    else:
        await ev.answer(text, reply_markup=markup)


async def notify_admins(bot: Bot, text: str, markup=None):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, reply_markup=markup)
        except Exception as e:
            log.warning("Cannot notify admin %s: %s", aid, e)


def pay_methods() -> list[tuple[str, str]]:
    out = []
    for line in (SET.get("payments") or DEFAULT_PAYMENTS).splitlines():
        if "|" in line:
            n, d = line.split("|", 1)
            if n.strip():
                out.append((n.strip(), d.strip()))
    return out


def min_deposit() -> float:
    try:
        return float(SET.get("min_dep", "20"))
    except ValueError:
        return 20.0


async def joined(bot: Bot, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(FORCE_CHANNEL, uid)
        return str(getattr(m.status, "value", m.status)) in ("member", "administrator", "creator")
    except Exception:
        return True


def home_text(uid: int) -> str:
    u = get_user(uid)
    t = (f"{E('fire')} <b>Welcome to {esc(BRAND)}</b>\n"
         f"<i>Premium Proxy &amp; VPN · Instant delivery · 24/7</i>\n\n"
         f"{E('profile')} <b>ID:</b> <code>{uid}</code>\n"
         f"{E('wallet')} <b>Balance:</b> {money(u['balance'])}\n")
    if SET.get("notice"):
        t += f"\n{E('notice')} <b>Notice</b>\n{SET['notice']}\n"
    return t + "\n<i>Use the menu below</i> 👇"


def rank_of(spent: float) -> str:
    if spent >= 5000:
        return "💎 Diamond"
    if spent >= 1000:
        return "🥇 Gold"
    if spent >= 200:
        return "🥈 Silver"
    return "🥉 Bronze"


# ═════════════ MIDDLEWARE ═════════════
async def refresh_menu(event, u, row):
    """If admin changed colors/emoji, push a fresh bottom keyboard to the user."""
    if not isinstance(event, Message):
        return
    mv = SET.get("mv", "0")
    if row.get("mv") != mv:
        row["mv"] = mv
        await put("users", u.id)
        if not (event.text or "").startswith("/start"):
            await event.answer("🔄 Menu updated", reply_markup=menu_kb(u.id))


class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if not u:
            return await handler(event, data)
        row = await touch_user(u)
        if u.id in ADMIN_IDS:
            await refresh_menu(event, u, row)
            return await handler(event, data)
        msg = event.message if isinstance(event, CallbackQuery) else event

        async def deny(text, markup=None):
            if isinstance(event, CallbackQuery):
                await event.answer()
            await msg.answer(text, reply_markup=markup)

        if row["banned"]:
            return await deny(f"{E('ban')} <b>You are banned from using this bot.</b>")
        if SET.get("maintenance") == "1":
            return await deny(f"{E('settings')} <b>Maintenance mode</b>\nWe'll be back shortly!")
        if FORCE_CHANNEL and not (isinstance(event, CallbackQuery) and event.data == "chk"):
            if not await joined(data["bot"], u.id):
                url = f"https://t.me/{FORCE_CHANNEL.lstrip('@')}"
                return await deny(f"{E('lock')} <b>Please join our channel first</b>\nJoin, then tap <b>I've Joined</b>.",
                                  kb([B("Join Channel", url=url, key="join", fb="📢")],
                                     [B("I've Joined", "chk", "join")]))
        await refresh_menu(event, u, row)
        return await handler(event, data)


# ═════════════ USER SCREENS ═════════════
async def screen_buy(ev):
    cats = sorted(D["cats"].values(), key=lambda c: c["id"])
    t = f"{E('cart')} <b>Buy Products</b>\n\nSelect a category:"
    if not cats:
        t += "\n\n<i>No categories yet.</i>"
    btns, row = [], []
    for c in cats:
        row.append(B(c["name"], f"cat:{c['id']}", "cat", cid=c.get("emoji"), fb=c.get("fb", "📁")))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([B("Cancel", "close", "cancel")])
    await show(ev, t, kb(*btns))


async def screen_cat(ev, cid):
    c = D["cats"].get(str(cid))
    if not c:
        return await show(ev, "Category not found.")
    rows = sorted((product_row(p["id"]) for p in D["products"].values()
                   if str(p["category"]) == str(cid) and p.get("active")), key=lambda p: p["id"])
    t = f"{E('cart')} <b>Category: {esc(c['name'])}</b>\n\n"
    t += "Select a product:" if rows else "<i>No products available right now. Please check back soon.</i>"
    btns = []
    for p in rows:
        label = f"{p['name']} ❯ {money(p['price'])}" if p["stk"] else f"{p['name']} ❯ Sold out"
        btns.append([B(label, f"p:{p['id']}", "item", cid=p.get("emoji"), fb=p.get("fb"))])
    btns.append([B("Back to Categories", "buy", "back", fb="⬇️")])
    await show(ev, t, kb(*btns))


async def screen_profile(ev):
    uid = ev.from_user.id
    u = get_user(uid)
    n = sum(1 for o in D["orders"].values() if o["user_id"] == uid)
    dep = sum(d["amount"] for d in D["deposits"].values() if d["user_id"] == uid and d["status"] == "approved")
    un = f"@{esc(u['username'])}" if u.get("username") else "—"
    t = (f"{E('profile')} <b>My Profile</b>\n{BAR}\n"
         f"🆔 <b>ID:</b> <code>{uid}</code>\n"
         f"📛 <b>Name:</b> {esc(u.get('full_name') or '-')}\n"
         f"🔗 <b>Username:</b> {un}\n{BAR}\n"
         f"{E('wallet')} <b>Balance:</b> {money(u['balance'])}\n"
         f"{E('cart')} <b>Total Spent:</b> {money(u['spent'])}\n"
         f"{E('money')} <b>Total Deposited:</b> {money(dep)}\n"
         f"{E('orders')} <b>Orders:</b> {n}\n{BAR}\n"
         f"🏅 <b>Rank:</b> {rank_of(u['spent'])}\n"
         f"{E('date')} <b>Member since:</b> {fdate(u['joined_at'])}")
    await show(ev, t, kb([B("My Orders", "ords:0", "my_orders"), B("Add Funds", "dep", "deposit")]))


async def screen_orders(ev, page=0):
    uid = ev.from_user.id
    allo = sorted((o for o in D["orders"].values() if o["user_id"] == uid), key=lambda o: -o["id"])
    if not allo:
        return await show(ev, f"{E('orders')} <b>My Orders</b>\n\n<i>You have no orders yet.</i>",
                          kb([B("Buy Product", "buy", "buy")]))
    items, page, pages = paginate(allo, page, 8)
    btns = [[B(f"#{o['id']} · {o['product_name']} · {money(o['price'])}", f"o:{o['id']}", "order_btn")] for o in items]
    nr = nav_row("ords", page, pages)
    if nr:
        btns.append(nr)
    await show(ev, f"{E('orders')} <b>My Orders</b>  <i>({len(allo)} total)</i>\nTap an order to open its invoice 👇", kb(*btns))


def invoice_text(o, head=None) -> str:
    u = get_user(o["user_id"]) or {}
    t = f"{head}\n\n" if head else ""
    t += (f"{E('invoice')} <b>INVOICE #{o['id']}</b>\n{BAR}\n"
          f"{E('date')} <b>Date:</b> {fdate(o['created_at'])}\n"
          f"{E('profile')} <b>Customer:</b> {esc(u.get('full_name') or '-')} (<code>{o['user_id']}</code>)\n"
          f"{E('stock')} <b>Product:</b> {esc(o['product_name'])}\n"
          f"{E('money')} <b>Paid:</b> {money(o['price'])}\n")
    if o.get("balance_after") is not None:
        t += f"{E('wallet')} <b>Remaining Balance:</b> {money(o['balance_after'])}\n"
    t += (f"{E('ok')} <b>Status:</b> Delivered\n{BAR}\n"
          f"{E('down')} <b>Your Details:</b>\n<code>{esc(o['data'])}</code>\n{BAR}\n"
          f"<i>Thank you for shopping with {esc(BRAND)}!</i>")
    return t


async def screen_support(ev):
    sup = SET.get("support", "").lstrip("@")
    t = ("💬 <b>Our Support Team</b>\n\nFor any issues or inquiries, click the button below to contact support directly.")
    if sup:
        return await show(ev, t, kb([B("Contact Admin", url=f"https://t.me/{sup}", key="contact", fb="💬")]))
    await show(ev, t + "\n\n<i>Support contact has not been set yet.</i>")


async def screen_code(ev):
    t = SET.get("code_text") or ("🔑 <b>Get Code</b>\n\nOpen the code page below and enter your account to receive "
                                  "your verification code.")
    url = SET.get("code_url", "")
    if url and not url.startswith("http"):
        url = f"https://t.me/{url.lstrip('@')}"
    await show(ev, t, kb([B("Get Code", url=url, key="contact", fb="🔑")]) if url else None)


async def screen_dep(ev):
    methods = pay_methods()
    if not methods:
        return await show(ev, "Deposits are currently unavailable.")
    btns = [[B(n, f"depm:{i}", "pay")] for i, (n, _) in enumerate(methods)]
    btns.append([B("Cancel", "close", "cancel")])
    await show(ev, f"{E('money')} <b>Add Funds</b>\n\nChoose a payment method 👇\n"
                   f"<i>(Minimum: {money(min_deposit())})</i>", kb(*btns))


# ═════════════ MENU ROUTER (bottom buttons always win over FSM states) ═════════════
menu = Router()


@menu.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    ADMIN_MODE.discard(m.from_user.id)
    await m.answer(home_text(m.from_user.id), reply_markup=menu_kb(m.from_user.id))


@menu.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(home_text(m.from_user.id), reply_markup=menu_kb(m.from_user.id))


@menu.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    ADMIN_MODE.add(m.from_user.id)
    await m.answer(f"{E('crown')} <b>Admin Panel</b>\n<i>Use the bottom buttons to manage your store.</i>",
                   reply_markup=admin_menu_kb())


@menu.message(F.text.func(lambda t: menu_key(t) in USER_KEYS))
async def on_user_menu(m: Message, state: FSMContext):
    await state.clear()
    k = menu_key(m.text)
    if k == "buy":
        await screen_buy(m)
    elif k == "my_profile":
        await screen_profile(m)
    elif k == "deposit":
        await screen_dep(m)
    elif k == "code":
        await screen_code(m)
    elif k == "support":
        await screen_support(m)
    elif k == "admin" and m.from_user.id in ADMIN_IDS:
        ADMIN_MODE.add(m.from_user.id)
        await m.answer(f"{E('crown')} <b>Admin Panel</b>\n<i>Use the bottom buttons to manage your store.</i>",
                       reply_markup=admin_menu_kb())


@menu.message(F.text.func(lambda t: menu_key(t) in ADMIN_KEYS), F.from_user.id.in_(ADMIN_IDS))
async def on_admin_menu(m: Message, state: FSMContext):
    await state.clear()
    k, uid = menu_key(m.text), m.from_user.id
    ADMIN_MODE.add(uid)
    if k == "a_exit":
        ADMIN_MODE.discard(uid)
        await m.answer("🏠 Back to user menu.", reply_markup=user_menu_kb(uid))
    elif k == "a_dash":
        await screen_stats(m)
    elif k == "a_users":
        await screen_users(m, 0)
    elif k == "a_prod":
        await screen_aprods(m, 0)
    elif k == "a_cat":
        await screen_acats(m)
    elif k == "a_deps":
        await screen_adeps(m)
    elif k == "a_bc":
        await state.set_state(Broadcast.msg)
        await m.answer(f"{E('broadcast')} <b>Broadcast</b>\n\nSend the message to deliver to <b>all users</b>.\n"
                       "<i>Text, photo, video, custom emoji - copied exactly as you send it.</i>",
                       reply_markup=kb([B("Cancel", "close", "x_no")]))
    elif k == "a_notice":
        await state.set_state(NoticeS.text)
        await m.answer(f"{E('notice')} <b>Notice Board</b>\n\nCurrent:\n{SET.get('notice') or '<i>(none)</i>'}\n\n"
                       "Send the new notice (formatting &amp; custom emoji supported).\nSend <code>clear</code> to remove it.",
                       reply_markup=kb([B("Cancel", "close", "x_no")]))
    elif k == "a_set":
        await screen_settings(m)
    elif k == "a_style":
        await screen_styles(m, 0)
    elif k == "a_emoji":
        await screen_emojis(m, 0)


# ═════════════ USER ROUTER ═════════════
router = Router()


class Dep(StatesGroup):
    amount = State()
    trx = State()


@router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@router.callback_query(F.data == "close")
async def cb_close(c: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.answer()


@router.callback_query(F.data == "chk")
async def cb_chk(c: CallbackQuery):
    if FORCE_CHANNEL and not await joined(c.bot, c.from_user.id):
        return await c.answer("You haven't joined yet!", show_alert=True)
    await c.message.answer(home_text(c.from_user.id), reply_markup=menu_kb(c.from_user.id))
    await c.answer()


@router.callback_query(F.data == "buy")
async def cb_buy_menu(c: CallbackQuery):
    await screen_buy(c)


@router.callback_query(F.data.startswith("cat:"))
async def cb_cat(c: CallbackQuery):
    await screen_cat(c, c.data.split(":")[1])


@router.callback_query(F.data == "dep")
async def cb_dep(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await screen_dep(c)


@router.callback_query(F.data.startswith("ords:"))
async def cb_ords(c: CallbackQuery):
    await screen_orders(c, int(c.data.split(":")[1]))


@router.callback_query(F.data.startswith("p:"))
async def cb_product(c: CallbackQuery):
    p = product_row(int(c.data.split(":")[1]))
    if not p or not p["active"]:
        return await c.answer("Product not available.", show_alert=True)
    t = (f"{E('stock')} <b>{esc(p['name'])}</b>\n\n{esc(p.get('description') or '')}\n\n"
         f"{E('money')} <b>Price:</b> {money(p['price'])}\n{E('stock')} <b>In stock:</b> {p['stk']}\n"
         f"{E('bolt')} <b>Delivery:</b> Instant")
    rows = []
    if p["stk"]:
        rows.append([B("Buy Now", f"buy:{p['id']}", "buynow")])
    rows.append([B("Back", f"cat:{p['category']}", "back")])
    await show(c, t, kb(*rows))


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery):
    p = product_row(int(c.data.split(":")[1]))
    u = get_user(c.from_user.id)
    if not p or not p["active"] or not p["stk"]:
        return await c.answer("Out of stock.", show_alert=True)
    if u["balance"] < p["price"]:
        t = (f"{E('no')} <b>Insufficient balance</b>\n\nPrice: {money(p['price'])}\n"
             f"Your balance: {money(u['balance'])}\n\nPlease add funds first.")
        return await show(c, t, kb([B("Add Funds", "dep", "deposit")], [B("Back", f"p:{p['id']}", "back")]))
    t = (f"{E('cart')} <b>Confirm purchase</b>\n\nProduct: <b>{esc(p['name'])}</b>\nPrice: <b>{money(p['price'])}</b>\n"
         f"Balance after: <b>{money(u['balance'] - p['price'])}</b>")
    await show(c, t, kb([B("Confirm", f"buyok:{p['id']}", "confirm"), B("Cancel", f"p:{p['id']}", "cancel")]))


@router.callback_query(F.data.startswith("buyok:"))
async def cb_buyok(c: CallbackQuery):
    pid, uid = int(c.data.split(":")[1]), c.from_user.id
    async with buy_lock:
        p, u = prod(pid), get_user(uid)
        if not p or not p["active"]:
            return await c.answer("Product not available.", show_alert=True)
        if u["balance"] < p["price"]:
            return await c.answer("Insufficient balance.", show_alert=True)
        items = [(int(k), s) for k, s in D["stock"].items() if int(s["pid"]) == pid and not s["sold"]]
        if not items:
            return await c.answer("Sorry, this product just went out of stock.", show_alert=True)
        sid, item = min(items, key=lambda x: x[0])
        item["sold"] = 1
        u["balance"] -= p["price"]
        u["spent"] += p["price"]
        oid = await nid("orders")
        o = {"id": oid, "user_id": uid, "product_id": pid, "product_name": p["name"], "price": p["price"],
             "data": item["data"], "balance_after": u["balance"], "created_at": int(time.time())}
        D["orders"][str(oid)] = o
        await put("stock", sid)
        await put("users", uid)
        await put("orders", oid)
    head = f"{E('party')} <b>{esc(p['name'])} purchase successful, Now enjoy!</b> {E('ok')}"
    await show(c, invoice_text(o, head), kb([B("My Orders", "ords:0", "my_orders"), B("Buy More", "buy", "buy")]))
    await notify_admins(c.bot, f"{E('cart')} <b>New order #{oid}</b>\nUser: <code>{uid}</code> "
                               f"(@{esc(c.from_user.username or '-')})\nProduct: {esc(p['name'])} · {money(p['price'])}")


@router.callback_query(F.data.startswith("o:"))
async def cb_order(c: CallbackQuery):
    o = D["orders"].get(c.data.split(":")[1])
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Order not found.", show_alert=True)
    await show(c, invoice_text(o), kb([B("Back", "ords:0", "back")]))


@router.callback_query(F.data.startswith("depm:"))
async def cb_depm(c: CallbackQuery, state: FSMContext):
    idx, methods = int(c.data.split(":")[1]), pay_methods()
    if idx >= len(methods):
        return await c.answer("Method not found.", show_alert=True)
    name, details = methods[idx]
    await state.set_state(Dep.amount)
    await state.update_data(method=name)
    await show(c, f"{E('wallet')} <b>{esc(name)}</b>\n\n<code>{esc(details)}</code>\n\n"
                  f"1️⃣ Send the money to the above account.\n"
                  f"2️⃣ Now type the <b>amount</b> you sent\n<i>(Minimum: {money(min_deposit())})</i>",
               kb([B("Cancel", "close", "cancel")]))


@router.message(StateFilter(Dep.amount))
async def dep_amount(m: Message, state: FSMContext):
    try:
        amt = float((m.text or "").replace(",", "").strip())
    except ValueError:
        return await m.answer("❗ Please send a valid number, e.g. <code>50</code>")
    if amt < min_deposit():
        return await m.answer(f"❗ Minimum deposit is <b>{money(min_deposit())}</b>")
    await state.update_data(amount=amt)
    await state.set_state(Dep.trx)
    await m.answer(f"{E('ok')} Amount: <b>{money(amt)}</b>\n\nNow send your <b>Transaction ID / reference</b>:",
                   reply_markup=kb([B("Cancel", "close", "cancel")]))


@router.message(StateFilter(Dep.trx))
async def dep_trx(m: Message, state: FSMContext):
    d, trx = await state.get_data(), (m.text or "").strip()
    if len(trx) < 4:
        return await m.answer("❗ Please send a valid Transaction ID.")
    did = await nid("deposits")
    D["deposits"][str(did)] = {"id": did, "user_id": m.from_user.id, "method": d["method"], "amount": d["amount"],
                               "trx": trx, "status": "pending", "created_at": int(time.time())}
    await put("deposits", did)
    await state.clear()
    await m.answer(f"{E('ok')} <b>Deposit request submitted!</b>\n\nRequest: <b>#{did}</b>\nMethod: {esc(d['method'])}\n"
                   f"Amount: {money(d['amount'])}\nTrx: <code>{esc(trx)}</code>\n\n"
                   f"⏳ Our team will verify it shortly. You'll be notified once approved.")
    await notify_admins(
        m.bot, f"{E('money')} <b>New deposit #{did}</b>\nUser: <code>{m.from_user.id}</code> "
               f"(@{esc(m.from_user.username or '-')})\nMethod: {esc(d['method'])}\n"
               f"Amount: <b>{money(d['amount'])}</b>\nTrx: <code>{esc(trx)}</code>",
        kb([B("Approve", f"adm:dok:{did}", "x_ok"), B("Reject", f"adm:dno:{did}", "x_no")]))


# ═════════════ ADMIN ROUTER ═════════════
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class AddP(StatesGroup):
    name = State(); desc = State(); price = State()
class AddC(StatesGroup):
    name = State()
class RenC(StatesGroup):
    name = State()
class AddStock(StatesGroup):
    lines = State()
class EditPrice(StatesGroup):
    price = State()
class FindUser(StatesGroup):
    query = State()
class Adjust(StatesGroup):
    amount = State()
class Broadcast(StatesGroup):
    msg = State()
class NoticeS(StatesGroup):
    text = State()
class SetVal(StatesGroup):
    value = State()
class EmojiS(StatesGroup):
    value = State()


def nav_back(cb="adm:x"):
    return [B("Close", "close", "x_nav", fb="✖️")]


# ── Dashboard ──
async def screen_stats(ev):
    day = int(time.time()) - 86400
    users, orders, deps = D["users"].values(), D["orders"].values(), D["deposits"].values()
    t = (f"{E('stats')} <b>Dashboard</b>\n{BAR}\n"
         f"{E('users')} Users: <b>{len(D['users'])}</b> (+{sum(1 for u in users if u['joined_at'] > day)} today)\n"
         f"{E('stock')} Products: <b>{len(D['products'])}</b> · Stock left: "
         f"<b>{sum(1 for s in D['stock'].values() if not s['sold'])}</b>\n"
         f"{E('orders')} Orders: <b>{len(D['orders'])}</b>\n"
         f"{E('cart')} Revenue: <b>{money(sum(o['price'] for o in orders))}</b> "
         f"(24h: {money(sum(o['price'] for o in orders if o['created_at'] > day))})\n"
         f"{E('money')} Deposits approved: <b>{money(sum(d['amount'] for d in deps if d['status'] == 'approved'))}</b>\n"
         f"⏳ Pending deposits: <b>{sum(1 for d in deps if d['status'] == 'pending')}</b>\n"
         f"{E('wallet')} Users' total balance: <b>{money(sum(u['balance'] for u in users))}</b>")
    await show(ev, t, kb([B("Refresh", "adm:stats", "x_edit", fb="🔄")]))


@admin.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    await screen_stats(c)


# ── Categories ──
async def screen_acats(ev):
    cats = sorted(D["cats"].values(), key=lambda c: c["id"])
    btns = []
    for c in cats:
        n = sum(1 for p in D["products"].values() if str(p["category"]) == str(c["id"]))
        btns.append([B(f"{c['name']} · {n} products", f"adm:c:{c['id']}", "x_item", cid=c.get("emoji"), fb=c.get("fb", "📁"))])
    btns.append([B("Add Category", "adm:addc", "x_add")])
    await show(ev, f"📁 <b>Categories</b>\n\nTap a category to edit it.", kb(*btns))


@admin.callback_query(F.data == "adm:cats")
async def adm_cats(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await screen_acats(c)


@admin.callback_query(F.data == "adm:addc")
async def adm_addc(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddC.name)
    await show(c, "📁 Send the <b>category name</b> (e.g. <code>Mail</code>)", kb([B("Cancel", "adm:cats", "x_no")]))


@admin.message(StateFilter(AddC.name))
async def addc_name(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    if not name:
        return await m.answer("❗ Send a name.")
    cid = await nid("cats")
    D["cats"][str(cid)] = {"id": cid, "name": name, "emoji": "", "fb": "📁"}
    await put("cats", cid)
    await state.clear()
    await m.answer(f"{E('ok')} Category <b>{esc(name)}</b> created.\nNow set its custom emoji if you like 👇",
                   reply_markup=kb([B("Set Emoji", f"adm:cem:{cid}", "x_edit", fb="⭐")],
                                   [B("Categories", "adm:cats", "x_nav")]))


@admin.callback_query(F.data.startswith("adm:c:"))
async def adm_cat(c: CallbackQuery, state: FSMContext):
    await state.clear()
    cid = c.data.split(":")[2]
    cat = D["cats"].get(cid)
    if not cat:
        return await c.answer("Not found", show_alert=True)
    n = sum(1 for p in D["products"].values() if str(p["category"]) == cid)
    t = (f"📁 <b>{esc(cat['name'])}</b>\nID: <code>{cid}</code> · Products: <b>{n}</b>\n"
         f"Custom emoji: <code>{cat.get('emoji') or 'none'}</code>")
    await show(c, t, kb(
        [B("Set Emoji", f"adm:cem:{cid}", "x_edit", fb="⭐"), B("Rename", f"adm:crn:{cid}", "x_edit")],
        [B("Delete", f"adm:cdel:{cid}", "x_no", fb="🗑")],
        [B("Back", "adm:cats", "x_nav")]))


@admin.callback_query(F.data.startswith("adm:crn:"))
async def adm_crn(c: CallbackQuery, state: FSMContext):
    await state.set_state(RenC.name)
    await state.update_data(cid=c.data.split(":")[2])
    await show(c, "✏️ Send the <b>new category name</b>:", kb([B("Cancel", "adm:cats", "x_no")]))


@admin.message(StateFilter(RenC.name))
async def crn_save(m: Message, state: FSMContext):
    d = await state.get_data()
    cat = D["cats"].get(d["cid"])
    if cat and (m.text or "").strip():
        cat["name"] = m.text.strip()
        await put("cats", d["cid"])
    await state.clear()
    await m.answer(f"{E('ok')} Renamed.", reply_markup=kb([B("Categories", "adm:cats", "x_nav")]))


@admin.callback_query(F.data.startswith("adm:cdel:"))
async def adm_cdel(c: CallbackQuery):
    cid = c.data.split(":")[2]
    if any(str(p["category"]) == cid for p in D["products"].values()):
        return await c.answer("Delete or move its products first.", show_alert=True)
    D["cats"].pop(cid, None)
    await put("cats", cid)
    await c.answer("Deleted ✅")
    await screen_acats(c)


# ── Products ──
async def screen_aprods(ev, page=0):
    rows = sorted((product_row(p["id"]) for p in D["products"].values()), key=lambda p: -p["id"])
    items, page, pages = paginate(rows, page, 10)
    btns = [[B(f"{'🟢' if p['active'] else '🔴'} #{p['id']} {p['name']} · {money(p['price'])} · {p['stk']}",
               f"adm:p:{p['id']}", "x_item", cid=p.get("emoji"), fb="")] for p in items]
    nr = nav_row("adm:pl", page, pages)
    if nr:
        btns.append(nr)
    btns.append([B("Add Product", "adm:addp", "x_add")])
    await show(ev, f"{E('stock')} <b>Products</b>  <i>(🟢 active · 🔴 hidden · stock)</i>", kb(*btns))


@admin.callback_query(F.data.startswith("adm:pl:"))
async def adm_pl(c: CallbackQuery):
    await screen_aprods(c, int(c.data.split(":")[2]))


@admin.callback_query(F.data == "adm:addp")
async def adm_addp(c: CallbackQuery, state: FSMContext):
    await state.clear()
    cats = sorted(D["cats"].values(), key=lambda x: x["id"])
    if not cats:
        return await show(c, "Create a category first.", kb([B("Add Category", "adm:addc", "x_add")]))
    btns, row = [], []
    for ct in cats:
        row.append(B(ct["name"], f"adm:addpc:{ct['id']}", "cat", cid=ct.get("emoji"), fb=ct.get("fb", "📁")))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([B("Cancel", "close", "x_no")])
    await show(c, f"{E('stock')} <b>Add Product</b>\n\nChoose a category:", kb(*btns))


@admin.callback_query(F.data.startswith("adm:addpc:"))
async def adm_addp_cat(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddP.name)
    await state.update_data(cat=int(c.data.split(":")[2]))
    await show(c, "✏️ Send the <b>product name</b>\n<i>e.g. Nord VPN 7 Day</i>", kb([B("Cancel", "close", "x_no")]))


@admin.message(StateFilter(AddP.name))
async def addp_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(AddP.desc)
    await m.answer("📝 Send the <b>description</b> (features, validity, region…)")


@admin.message(StateFilter(AddP.desc))
async def addp_desc(m: Message, state: FSMContext):
    await state.update_data(desc=m.text.strip())
    await state.set_state(AddP.price)
    await m.answer(f"{E('money')} Send the <b>price</b> (number, in {CURRENCY})")


@admin.message(StateFilter(AddP.price))
async def addp_price(m: Message, state: FSMContext):
    try:
        price = float(m.text.replace(",", "").strip())
    except ValueError:
        return await m.answer("❗ Send a valid number.")
    d = await state.get_data()
    pid = await nid("products")
    D["products"][str(pid)] = {"id": pid, "category": d["cat"], "name": d["name"], "description": d["desc"],
                               "price": price, "active": 1, "emoji": ""}
    await put("products", pid)
    await state.clear()
    await m.answer(f"{E('ok')} Product created!", reply_markup=kb(
        [B("Add Stock", f"adm:stk:{pid}", "x_add", fb="🗃"), B("Set Emoji", f"adm:pem:{pid}", "x_edit", fb="⭐")],
        [B("Products", "adm:pl:0", "x_nav")]))


async def render_admin_product(c: CallbackQuery, pid: int):
    p = product_row(pid)
    if not p:
        return await c.answer("Not found", show_alert=True)
    sold = sum(1 for s in D["stock"].values() if int(s["pid"]) == pid and s["sold"])
    t = (f"{E('stock')} <b>#{p['id']} {esc(p['name'])}</b>\nCategory: <b>{esc(cat_name(p['category']))}</b>\n\n"
         f"{esc(p.get('description') or '')}\n\nPrice: <b>{money(p['price'])}</b>\n"
         f"In stock: <b>{p['stk']}</b> · Sold: <b>{sold}</b>\n"
         f"Emoji: <code>{p.get('emoji') or 'none'}</code>\n"
         f"Status: {'🟢 Active' if p['active'] else '🔴 Hidden'}")
    await show(c, t, kb(
        [B("Add Stock", f"adm:stk:{pid}", "x_add", fb="🗃"), B("Edit Price", f"adm:ep:{pid}", "x_edit")],
        [B("Set Emoji", f"adm:pem:{pid}", "x_edit", fb="⭐"),
         B("Hide" if p["active"] else "Show", f"adm:tg:{pid}", "x_edit", fb="👁")],
        [B("Delete", f"adm:del:{pid}", "x_no", fb="🗑")],
        [B("Back", "adm:pl:0", "x_nav")]))


@admin.callback_query(F.data.startswith("adm:p:"))
async def adm_product(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_admin_product(c, int(c.data.split(":")[2]))


@admin.callback_query(F.data.startswith("adm:tg:"))
async def adm_toggle(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    p = prod(pid)
    if p:
        p["active"] = 0 if p["active"] else 1
        await put("products", pid)
    await render_admin_product(c, pid)


@admin.callback_query(F.data.startswith("adm:del:"))
async def adm_del(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    await show(c, "⚠️ <b>Delete this product and all its unsold stock?</b>",
               kb([B("Yes, delete", f"adm:delok:{pid}", "x_no", fb="🗑"), B("No", f"adm:p:{pid}", "x_ok")]))


@admin.callback_query(F.data.startswith("adm:delok:"))
async def adm_delok(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    dead = [k for k, s in D["stock"].items() if int(s["pid"]) == pid and not s["sold"]]
    for k in dead:
        del D["stock"][k]
    await put("stock", *dead)
    D["products"].pop(str(pid), None)
    await put("products", pid)
    await c.answer("Deleted ✅")
    await screen_aprods(c, 0)


@admin.callback_query(F.data.startswith("adm:ep:"))
async def adm_ep(c: CallbackQuery, state: FSMContext):
    await state.set_state(EditPrice.price)
    await state.update_data(pid=int(c.data.split(":")[2]))
    await show(c, "✏️ Send the <b>new price</b>:", kb([B("Cancel", "close", "x_no")]))


@admin.message(StateFilter(EditPrice.price))
async def ep_save(m: Message, state: FSMContext):
    try:
        price = float(m.text.replace(",", "").strip())
    except ValueError:
        return await m.answer("❗ Send a valid number.")
    d = await state.get_data()
    if prod(d["pid"]):
        prod(d["pid"])["price"] = price
        await put("products", d["pid"])
    await state.clear()
    await m.answer(f"{E('ok')} Price updated to <b>{money(price)}</b>",
                   reply_markup=kb([B("Back to product", f"adm:p:{d['pid']}", "x_nav")]))


@admin.callback_query(F.data.startswith("adm:stk:"))
async def adm_stk(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddStock.lines)
    await state.update_data(pid=int(c.data.split(":")[2]))
    await show(c, f"{E('stock')} <b>Add Stock</b>\n\nSend items - <b>one per line</b>.\n"
                  "For multi-line items, separate them with a line containing only <code>---</code>.\n\n"
                  "<i>Example:</i>\n<code>1.2.3.4:8080:user:pass\n5.6.7.8:8080:user:pass</code>",
               kb([B("Cancel", "close", "x_no")]))


@admin.message(StateFilter(AddStock.lines))
async def stk_save(m: Message, state: FSMContext):
    d, text = await state.get_data(), (m.text or "").strip()
    items = [x.strip() for x in re.split(r"\n-{3,}\n", text)] if re.search(r"\n-{3,}\n", text) \
        else [x.strip() for x in text.splitlines()]
    items = [x for x in items if x]
    if not items:
        return await m.answer("❗ Nothing to add.")
    keys = []
    for x in items:
        sid = await nid("stock")
        D["stock"][str(sid)] = {"pid": d["pid"], "data": x, "sold": 0}
        keys.append(sid)
    await put("stock", *keys)
    await state.clear()
    await m.answer(f"{E('ok')} Added <b>{len(items)}</b> items to stock.",
                   reply_markup=kb([B("Back to product", f"adm:p:{d['pid']}", "x_nav")]))


# ── Deposits ──
async def screen_adeps(ev):
    rows = sorted((d for d in D["deposits"].values() if d["status"] == "pending"), key=lambda d: d["id"])[:20]
    if not rows:
        return await show(ev, f"{E('money')} <b>Pending Deposits</b>\n\n<i>Nothing pending 🎉</i>")
    btns = [[B(f"#{d['id']} · {money(d['amount'])} · {d['method']} · {d['user_id']}", f"adm:d:{d['id']}", "x_item", fb="⏳")]
            for d in rows]
    await show(ev, f"{E('money')} <b>Pending Deposits</b>", kb(*btns))


@admin.callback_query(F.data == "adm:deps")
async def adm_deps(c: CallbackQuery):
    await screen_adeps(c)


@admin.callback_query(F.data.startswith("adm:d:"))
async def adm_dep(c: CallbackQuery):
    d = D["deposits"].get(c.data.split(":")[2])
    if not d:
        return await c.answer("Not found", show_alert=True)
    t = (f"{E('money')} <b>Deposit #{d['id']}</b>\nUser: <code>{d['user_id']}</code>\nMethod: {esc(d['method'])}\n"
         f"Amount: <b>{money(d['amount'])}</b>\nTrx: <code>{esc(d['trx'])}</code>\nStatus: <b>{d['status']}</b>\n"
         f"Date: {fdate(d['created_at'])}")
    rows = []
    if d["status"] == "pending":
        rows.append([B("Approve", f"adm:dok:{d['id']}", "x_ok"), B("Reject", f"adm:dno:{d['id']}", "x_no")])
    rows.append([B("Back", "adm:deps", "x_nav")])
    await show(c, t, kb(*rows))


@admin.callback_query(F.data.startswith("adm:dok:"))
async def adm_dok(c: CallbackQuery):
    did = c.data.split(":")[2]
    async with buy_lock:
        d = D["deposits"].get(did)
        if not d or d["status"] != "pending":
            return await c.answer("Already handled.", show_alert=True)
        d["status"] = "approved"
        u = get_user(d["user_id"])
        if u:
            u["balance"] += d["amount"]
            await put("users", d["user_id"])
        await put("deposits", did)
    try:
        await c.message.edit_text(c.message.html_text + f"\n\n{E('ok')} <b>Approved by {esc(c.from_user.full_name)}</b>")
    except Exception:
        pass
    await c.answer("Approved ✅")
    try:
        await c.bot.send_message(
            d["user_id"], f"{E('ok')} <b>Deposit Approved!</b>\nAmount: <b>{money(d['amount'])}</b>\n"
                          f"New balance: <b>{money(u['balance'])}</b>",
            reply_markup=kb([B("Buy Product", "buy", "buy")]))
    except Exception:
        pass


@admin.callback_query(F.data.startswith("adm:dno:"))
async def adm_dno(c: CallbackQuery):
    did = c.data.split(":")[2]
    d = D["deposits"].get(did)
    if not d or d["status"] != "pending":
        return await c.answer("Already handled.", show_alert=True)
    d["status"] = "rejected"
    await put("deposits", did)
    try:
        await c.message.edit_text(c.message.html_text + f"\n\n{E('no')} <b>Rejected by {esc(c.from_user.full_name)}</b>")
    except Exception:
        pass
    await c.answer("Rejected ❌")
    try:
        await c.bot.send_message(d["user_id"], f"{E('no')} <b>Deposit #{did} rejected</b>\n"
                                               "If you think this is a mistake, please contact support.")
    except Exception:
        pass


# ── Users (list + manage) ──
async def screen_users(ev, page=0):
    rows = sorted(D["users"].values(), key=lambda u: -u["joined_at"])
    items, page, pages = paginate(rows, page, 8)
    btns = []
    for u in items:
        name = (u.get("full_name") or "-")[:18]
        un = f" @{u['username']}" if u.get("username") else ""
        btns.append([B(f"{'🚫 ' if u['banned'] else ''}{name}{un} · {money(u['balance'])}", f"adm:u:{u['id']}", "x_item", fb="👤")])
    nr = nav_row("adm:ul", page, pages)
    if nr:
        btns.append(nr)
    btns.append([B("Search User", "adm:find", "x_edit", fb="🔎")])
    banned = sum(1 for u in rows if u["banned"])
    await show(ev, f"{E('users')} <b>Users</b>\nTotal: <b>{len(rows)}</b> · Banned: <b>{banned}</b>\n"
                   f"<i>Newest first - tap a user to manage</i>", kb(*btns))


@admin.callback_query(F.data.startswith("adm:ul:"))
async def adm_ul(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await screen_users(c, int(c.data.split(":")[2]))


@admin.callback_query(F.data == "adm:find")
async def adm_find(c: CallbackQuery, state: FSMContext):
    await state.set_state(FindUser.query)
    await show(c, "🔎 Send a <b>user ID</b> or <b>@username</b>:", kb([B("Cancel", "close", "x_no")]))


def user_panel(uid: int):
    u = get_user(uid)
    if not u:
        return None, None
    n = sum(1 for o in D["orders"].values() if o["user_id"] == uid)
    un = f"@{esc(u['username'])}" if u.get("username") else "—"
    t = (f"{E('profile')} <b>{esc(u.get('full_name') or '-')}</b>\n{BAR}\n"
         f"🆔 <code>{uid}</code> · {un}\n{E('wallet')} Balance: <b>{money(u['balance'])}</b>\n"
         f"{E('cart')} Spent: <b>{money(u['spent'])}</b>\n{E('orders')} Orders: <b>{n}</b>\n"
         f"🏅 Rank: {rank_of(u['spent'])}\n{E('date')} Joined: {fdate(u['joined_at'])}\n"
         f"Status: {'🚫 Banned' if u['banned'] else '✅ Active'}")
    return t, kb(
        [B("Add Balance", f"adm:adj:{uid}:+", "x_ok", fb="➕"), B("Deduct", f"adm:adj:{uid}:-", "x_no", fb="➖")],
        [B("Orders", f"adm:uo:{uid}", "x_edit", fb="📦"), B("Unban" if u["banned"] else "Ban", f"adm:ban:{uid}", "x_no", fb="🚫")],
        [B("Users", "adm:ul:0", "x_nav")])


@admin.callback_query(F.data.regexp(r"^adm:u:\d+$"))
async def adm_user(c: CallbackQuery, state: FSMContext):
    await state.clear()
    t, mk = user_panel(int(c.data.split(":")[2]))
    if not t:
        return await c.answer("Not found", show_alert=True)
    await show(c, t, mk)


@admin.message(StateFilter(FindUser.query))
async def find_user(m: Message, state: FSMContext):
    q = (m.text or "").strip().lstrip("@")
    row = get_user(q) if q.isdigit() else next(
        (u for u in D["users"].values() if (u.get("username") or "").lower() == q.lower()), None)
    if not row:
        return await m.answer("❗ User not found. Try again or /cancel")
    await state.clear()
    t, mk = user_panel(row["id"])
    await m.answer(t, reply_markup=mk)


@admin.callback_query(F.data.startswith("adm:ban:"))
async def adm_ban(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    u = get_user(uid)
    if u:
        u["banned"] = 0 if u["banned"] else 1
        await put("users", uid)
    t, mk = user_panel(uid)
    await show(c, t, mk)


@admin.callback_query(F.data.startswith("adm:uo:"))
async def adm_uo(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    rows = sorted((o for o in D["orders"].values() if o["user_id"] == uid), key=lambda o: -o["id"])[:15]
    if not rows:
        return await c.answer("No orders.", show_alert=True)
    btns = [[B(f"#{o['id']} · {o['product_name']} · {money(o['price'])}", f"adm:o:{o['id']}", "x_item", fb="🧾")] for o in rows]
    btns.append([B("Back", f"adm:u:{uid}", "x_nav")])
    await show(c, f"{E('orders')} <b>Orders of <code>{uid}</code></b> <i>(last 15)</i>", kb(*btns))


@admin.callback_query(F.data.startswith("adm:o:"))
async def adm_o(c: CallbackQuery):
    o = D["orders"].get(c.data.split(":")[2])
    if not o:
        return await c.answer("Not found", show_alert=True)
    await show(c, invoice_text(o), kb([B("Back", f"adm:uo:{o['user_id']}", "x_nav")]))


@admin.callback_query(F.data.startswith("adm:adj:"))
async def adm_adj(c: CallbackQuery, state: FSMContext):
    _, _, uid, sign = c.data.split(":")
    await state.set_state(Adjust.amount)
    await state.update_data(uid=int(uid), sign=sign)
    await show(c, f"Send the amount to <b>{'add to' if sign == '+' else 'deduct from'}</b> the balance:",
               kb([B("Cancel", "close", "x_no")]))


@admin.message(StateFilter(Adjust.amount))
async def adj_save(m: Message, state: FSMContext):
    try:
        amt = abs(float(m.text.replace(",", "").strip()))
    except ValueError:
        return await m.answer("❗ Send a valid number.")
    d = await state.get_data()
    delta = amt if d["sign"] == "+" else -amt
    get_user(d["uid"])["balance"] += delta
    await put("users", d["uid"])
    await state.clear()
    t, mk = user_panel(d["uid"])
    await m.answer(f"{E('ok')} Balance updated.\n\n{t}", reply_markup=mk)
    try:
        await m.bot.send_message(d["uid"], f"{E('wallet')} Your balance was adjusted by "
                                           f"<b>{'+' if delta >= 0 else '-'}{money(abs(delta))}</b> by admin.")
    except Exception:
        pass


# ── Broadcast & Notice ──
@admin.message(StateFilter(Broadcast.msg))
async def bc_preview(m: Message, state: FSMContext):
    n = sum(1 for u in D["users"].values() if not u["banned"])
    await state.update_data(chat=m.chat.id, mid=m.message_id)
    await m.answer(f"{E('broadcast')} Ready to send to <b>{n}</b> users.\nPreview is the message above ☝️",
                   reply_markup=kb([B(f"Send to {n} users", "adm:bcgo", "x_ok", fb="📤")],
                                   [B("Cancel", "close", "x_no")]))


@admin.callback_query(F.data == "adm:bcgo")
async def bc_go(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("mid"):
        return await c.answer("Nothing to send.", show_alert=True)
    await state.clear()
    users = [u["id"] for u in D["users"].values() if not u["banned"]]
    await show(c, f"📤 Sending to {len(users)} users…")
    ok = fail = 0
    for uid in users:
        for _ in range(2):
            try:
                await c.bot.copy_message(uid, d["chat"], d["mid"])
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.05)
    await c.message.answer(f"{E('ok')} <b>Broadcast finished</b>\n\nDelivered: <b>{ok}</b>\nFailed: <b>{fail}</b>")


@admin.message(StateFilter(NoticeS.text))
async def notice_save(m: Message, state: FSMContext):
    await state.clear()
    if (m.text or "").strip().lower() == "clear":
        await del_setting("notice")
        return await m.answer(f"{E('ok')} Notice cleared.")
    await set_setting("notice", m.html_text)
    await m.answer(f"{E('ok')} Notice updated! (shown on /start)")


# ── Settings ──
SETTING_PROMPTS = {
    "support": "Send the <b>support username</b> (e.g. <code>@MySupport</code>). Send <code>clear</code> to remove.",
    "payments": "Send payment methods - <b>one per line</b> as <code>Name|Details</code>:\n"
                "<code>bKash|017XXXXXXXX (Send Money)\nBinance Pay|XXXXXXXX</code>",
    "min_dep": f"Send the <b>minimum deposit</b> amount (number, {CURRENCY}).",
    "code_text": "Send the <b>Get Code</b> message text (HTML allowed). Send <code>clear</code> for default.",
    "code_url": "Send the <b>Get Code link</b> (https://… or @username). Send <code>clear</code> to remove.",
}


async def screen_settings(ev):
    mnt = SET.get("maintenance") == "1"
    t = (f"{E('settings')} <b>Settings</b>\n{BAR}\nSupport: <b>{esc(SET.get('support') or '-')}</b>\n"
         f"Min deposit: <b>{money(min_deposit())}</b>\nPayment methods: <b>{len(pay_methods())}</b>\n"
         f"Get Code link: <b>{esc(SET.get('code_url') or '-')}</b>\n"
         f"Maintenance: <b>{'ON 🔴' if mnt else 'OFF 🟢'}</b>")
    await show(ev, t, kb(
        [B("Support", "adm:sv:support", "x_edit", fb="💬"), B("Min Deposit", "adm:sv:min_dep", "x_edit", fb="💰")],
        [B("Payment Methods", "adm:sv:payments", "x_edit", fb="💳")],
        [B("Code Text", "adm:sv:code_text", "x_edit", fb="🔑"), B("Code Link", "adm:sv:code_url", "x_edit", fb="🔗")],
        [B("Maintenance: ON" if mnt else "Maintenance: OFF", "adm:mnt", "x_no" if mnt else "x_item", fb="🛠")]))


@admin.callback_query(F.data == "adm:set")
async def adm_set(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await screen_settings(c)


@admin.callback_query(F.data == "adm:mnt")
async def adm_mnt(c: CallbackQuery):
    await set_setting("maintenance", "0" if SET.get("maintenance") == "1" else "1")
    await screen_settings(c)


@admin.callback_query(F.data.startswith("adm:sv:"))
async def adm_sv(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.set_state(SetVal.value)
    await state.update_data(key=key)
    await show(c, SETTING_PROMPTS[key], kb([B("Cancel", "adm:set", "x_no")]))


@admin.message(StateFilter(SetVal.value))
async def sv_save(m: Message, state: FSMContext):
    d = await state.get_data()
    key, val = d["key"], (m.html_text if d["key"] == "code_text" else (m.text or "")).strip()
    if val.lower() == "clear":
        await del_setting(key)
    else:
        if key == "min_dep":
            try:
                float(val)
            except ValueError:
                return await m.answer("❗ Send a valid number.")
        if key == "support":
            val = val.lstrip("@")
        await set_setting(key, val)
    await state.clear()
    await m.answer(f"{E('ok')} Saved.", reply_markup=kb([B("Settings", "adm:set", "x_nav")]))


# ── Button colors (user + admin buttons) ──
async def screen_styles(ev, page=0):
    keys = list(BTN)
    items, page, pages = paginate(keys, page, 10)
    btns = []
    for k in items:
        label, _, _, grp = BTN[k]
        btns.append([B(f"{label}  [{'User' if grp == 'user' else 'Admin'}] {STYLE_MARK[bstyle(k)]}", f"adm:stk:{k}", k)])
    nr = nav_row("adm:sty", page, pages)
    if nr:
        btns.append(nr)
    await show(ev, f"🎨 <b>Button Colors</b>\n\nPick a button to change its color.\n"
                   f"Buttons below already show their <b>current color</b>.\n"
                   f"<i>Changes reach every user's menu automatically.</i>", kb(*btns))


@admin.callback_query(F.data.startswith("adm:sty:"))
async def adm_sty(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await screen_styles(c, int(c.data.split(":")[2]))


@admin.callback_query(F.data.startswith("adm:stk:"))
async def adm_stk_pick(c: CallbackQuery):
    k = c.data.split(":")[2]
    if k not in BTN:
        return await c.answer("Unknown", show_alert=True)
    cur = bstyle(k)
    t = (f"🎨 <b>{BTN[k][0]}</b>\nCurrent: <b>{STYLE_LABEL.get(cur or 'none')}</b>\n\nChoose a new color:")
    await show(c, t, kb(
        [B("Blue", f"adm:sts:{k}:primary", st="primary", fb="🔵"), B("Green", f"adm:sts:{k}:success", st="success", fb="🟢")],
        [B("Red", f"adm:sts:{k}:danger", st="danger", fb="🔴"), B("Default", f"adm:sts:{k}:none", fb="⚪")],
        [B("Reset to original", f"adm:sts:{k}:reset", "x_edit", fb="♻️")],
        [B("Back", "adm:sty:0", "x_nav")]))


@admin.callback_query(F.data.startswith("adm:sts:"))
async def adm_sts(c: CallbackQuery):
    _, _, k, val = c.data.split(":")
    if k not in BTN:
        return await c.answer("Unknown", show_alert=True)
    if val == "reset":
        await del_setting(f"style:{k}")
    else:
        await set_setting(f"style:{k}", val)
    await bump_menu()
    await c.answer("Color saved ✅")
    await c.message.answer("🔄 Menu refreshed", reply_markup=menu_kb(c.from_user.id))
    await adm_stk_pick(c)


# ── Custom emoji (all buttons + message icons) ──
def emoji_slots():
    s = [(k, BTN[k][0], BTN[k][2]) for k in BTN]
    s += [(k, f"msg: {k}", v) for k, v in EMOJI.items()]
    return s


async def screen_emojis(ev, page=0):
    items, page, pages = paginate(emoji_slots(), page, 12)
    btns, row = [], []
    for k, label, ch in items:
        row.append(B(f"{label[:16]} {'✅' if emoji_id(k) else ''}", f"adm:emk:{k}", None, fb=ch))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    nr = nav_row("adm:emo", page, pages)
    if nr:
        btns.append(nr)
    await show(ev, f"{E('star')} <b>Custom Emoji</b>\n\nPick a slot, then <b>send one custom emoji</b> "
                   "(from any Premium pack) - the bot reads its ID automatically. You can also send a numeric ID "
                   "or <code>clear</code>.\n\n✅ = custom emoji set\n"
                   "<i>Category &amp; product emojis: open them from Categories / Products.</i>", kb(*btns))


@admin.callback_query(F.data.startswith("adm:emo:"))
async def adm_emo(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await screen_emojis(c, int(c.data.split(":")[2]))


async def ask_emoji(c: CallbackQuery, state: FSMContext, kind, ref, title, back):
    await state.set_state(EmojiS.value)
    await state.update_data(kind=kind, ref=ref, back=back)
    await show(c, f"⭐ <b>{esc(title)}</b>\n\nSend a <b>custom emoji</b> now (or its numeric ID / <code>clear</code>).",
               kb([B("Cancel", back, "x_no")]))


@admin.callback_query(F.data.startswith("adm:emk:"))
async def adm_emk(c: CallbackQuery, state: FSMContext):
    k = c.data.split(":")[2]
    await ask_emoji(c, state, "slot", k, f"{BTN[k][0] if k in BTN else k}  (current: {emoji_id(k) or 'none'})", "adm:emo:0")


@admin.callback_query(F.data.startswith("adm:cem:"))
async def adm_cem(c: CallbackQuery, state: FSMContext):
    cid = c.data.split(":")[2]
    await ask_emoji(c, state, "cat", cid, f"Category emoji: {cat_name(cid)}", f"adm:c:{cid}")


@admin.callback_query(F.data.startswith("adm:pem:"))
async def adm_pem(c: CallbackQuery, state: FSMContext):
    pid = c.data.split(":")[2]
    await ask_emoji(c, state, "prod", pid, f"Product emoji: {(prod(pid) or {}).get('name', '?')}", f"adm:p:{pid}")


@admin.message(StateFilter(EmojiS.value))
async def emo_save(m: Message, state: FSMContext):
    d = await state.get_data()
    kind, ref, back = d["kind"], d["ref"], d["back"]
    cid = next((e.custom_emoji_id for e in (m.entities or []) if e.type == "custom_emoji"), None)
    txt = (m.text or "").strip()
    clear = txt.lower() == "clear"
    if not clear and not cid and txt.isdigit():
        cid = txt
    if not clear and not cid:
        return await m.answer("❗ I couldn't find a custom emoji. Send a <b>custom emoji</b> or a numeric ID.")
    val = "" if clear else cid
    if kind == "slot":
        await (del_setting(f"emoji:{ref}") if clear else set_setting(f"emoji:{ref}", val))
        await bump_menu()
    elif kind == "cat" and ref in D["cats"]:
        D["cats"][ref]["emoji"] = val
        await put("cats", ref)
    elif kind == "prod" and prod(ref):
        prod(ref)["emoji"] = val
        await put("products", ref)
    await state.clear()
    await m.answer(f"{E('ok')} {'Cleared' if clear else 'Saved'}: <code>{val or '-'}</code>",
                   reply_markup=menu_kb(m.from_user.id))
    await m.answer("Continue:", reply_markup=kb([B("Back", back, "x_nav")]))


# ═════════════ MAIN ═════════════
async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN is missing. Put it in .env")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty - nobody can open the admin panel!")
    await asyncio.to_thread(init_store)
    await seed_cats()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Guard())
    dp.callback_query.outer_middleware(Guard())
    dp.include_router(menu)      # bottom buttons first (they cancel any FSM state)
    dp.include_router(admin)
    dp.include_router(router)

    await bot.set_my_commands([BotCommand(command="start", description="Open main menu"),
                               BotCommand(command="cancel", description="Cancel current action")])
    log.info("Bot started as @%s", (await bot.get_me()).username)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass