# -*- coding: utf-8 -*-
"""
Premium Telegram Proxy & VPN Shop Bot  (Firebase edition)
---------------------------------------------------------
* Bottom (persistent) menu buttons for users  +  "Admin Panel" button for admins
* Colored buttons (primary/success/danger) + custom emoji on buttons & messages
* Data stored in Firebase Realtime Database (falls back to local data.json)
* Wallet with manual deposits, instant stock delivery, full admin panel

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
BRAND = os.getenv("BRAND_NAME", "PremiumNet")
CURRENCY = os.getenv("CURRENCY", "$")
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "").strip()
FIREBASE_CRED = os.getenv("FIREBASE_CRED", "firebase.json")      # service-account json
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "").strip()       # https://xxx-default-rtdb.firebaseio.com
STORE_FILE = os.getenv("STORE_FILE", "data.json")                # used only if Firebase is not set

EMOJI = {
    "fire": "🔥", "proxy": "5336972142066047577", "vpn": "5337255927735163754", "wallet": "💳", "orders": "📦",
    "support": "💬", "notice": "📢", "back": "⬅️", "admin": "👑", "stats": "📊",
    "add": "➕", "broadcast": "5424818078833715060", "users": "👥", "deposit": "💰",
    "settings": "⚙️", "ok": "✅", "no": "❌", "home": "🏠", "profile": "👤",
    "star": "⭐", "lock": "🔒", "stock": "🗃", "edit": "✏️", "trash": "🗑",
    "ban": "🚫", "bolt": "⚡", "gift": "🎁", "cart": "🛒",
}
DEFAULT_EMOJI_IDS: dict[str, str] = {}   # e.g. {"fire": "5368324170671202286"}

DEFAULT_PAYMENTS = (
    "bKash|01XXXXXXXXX (Send Money)\n"
    "Nagad|01XXXXXXXXX (Send Money)\n"
    "USDT TRC20|TXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("shop")

# ═════════════ STORAGE (in-memory cache + Firebase write-through) ═════════════
D: dict[str, dict] = {k: {} for k in ("users", "products", "stock", "orders", "deposits", "settings", "seq")}
SET = D["settings"]
fb_ref = None
store_lock = asyncio.Lock()
buy_lock = asyncio.Lock()


def _norm(v) -> dict:
    if isinstance(v, list):          # Firebase turns dense int keys into lists
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
        fb_ref.child(coll).update({k: D[coll].get(k) for k in keys})   # None => delete
    else:
        with open(STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(D, f, ensure_ascii=False)


async def put(coll: str, *keys):
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


def get_user(uid):
    return D["users"].get(str(uid))


def prod(pid):
    return D["products"].get(str(pid))


def stk_left(pid) -> int:
    return sum(1 for s in D["stock"].values() if int(s["pid"]) == int(pid) and not s["sold"])


def product_row(pid):
    p = prod(pid)
    return {**p, "stk": stk_left(pid)} if p else None


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


# ═════════════ HELPERS / UI ═════════════
esc = html.escape


def money(x) -> str:
    return f"{CURRENCY}{float(x):,.2f}"


def fdate(ts) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%d %b %Y, %H:%M")


def emoji_id(key: str) -> str:
    return SET.get(f"emoji:{key}") or DEFAULT_EMOJI_IDS.get(key, "")


def E(key: str) -> str:
    cid, fb = emoji_id(key), EMOJI.get(key, "•")
    return f'<tg-emoji emoji-id="{cid}">{fb}</tg-emoji>' if cid else fb


def B(text, cb=None, style=None, emoji=None, url=None) -> InlineKeyboardButton:
    kw = {}
    if style:
        kw["style"] = style
    label = text
    if emoji:
        cid = emoji_id(emoji)
        if cid:
            kw["icon_custom_emoji_id"] = cid
        else:
            label = f"{EMOJI.get(emoji, '')} {text}"
    if url:
        return InlineKeyboardButton(text=label, url=url, **kw)
    return InlineKeyboardButton(text=label, callback_data=cb, **kw)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


# ── Bottom menu (reply keyboard) ──
MENU = {  # key: (text, style)
    "proxy": ("Buy Proxy", "primary"), "vpn": ("Buy VPN", "primary"),
    "deposit": ("Add Funds", "success"), "wallet": ("My Wallet", None),
    "orders": ("My Orders", None), "notice": ("Notice", None),
    "support": ("Support", None), "admin": ("Admin Panel", "danger"),
}


def RB(key: str) -> KeyboardButton:
    text, style = MENU[key]
    kw, label = {}, text
    if style:
        kw["style"] = style
    cid = emoji_id(key)
    if cid:
        kw["icon_custom_emoji_id"] = cid
    else:
        label = f"{EMOJI[key]} {text}"
    return KeyboardButton(text=label, **kw)


def menu_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [[RB("proxy"), RB("vpn")], [RB("deposit"), RB("wallet")],
            [RB("orders"), RB("notice")], [RB("support")]]
    if uid in ADMIN_IDS:
        rows.append([RB("admin")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Choose an option 👇")


def menu_key(text):
    t = (text or "").strip()
    for k, (name, _) in MENU.items():
        if t in (name, f"{EMOJI[k]} {name}"):
            return k
    return None


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
        return float(SET.get("min_dep", "1"))
    except ValueError:
        return 1.0


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


def home_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [[B("Buy Proxy", "cat:proxy", "primary", "proxy"), B("Buy VPN", "cat:vpn", "primary", "vpn")],
            [B("Add Funds", "dep", "success", "deposit"), B("My Wallet", "wallet", None, "wallet")],
            [B("My Orders", "orders", None, "orders")]]
    if uid in ADMIN_IDS:
        rows.append([B("Admin Panel", "adm:home", "danger", "admin")])
    return kb(*rows)


# ═════════════ MIDDLEWARE ═════════════
class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if not u:
            return await handler(event, data)
        row = await touch_user(u)
        if u.id in ADMIN_IDS:
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
                                  kb([B("Join Channel", url=url, style="primary", emoji="notice")],
                                     [B("I've Joined", "chk", "success", "ok")]))
        return await handler(event, data)


# ═════════════ SCREENS (work for Message and CallbackQuery) ═════════════
CAT_NAMES = {"proxy": "Proxy", "vpn": "VPN"}


async def screen_cat(ev, cat):
    rows = sorted((product_row(p["id"]) for p in D["products"].values()
                   if p["category"] == cat and p.get("active")), key=lambda p: p["id"])
    t = f"{E(cat)} <b>{CAT_NAMES.get(cat, cat)} Plans</b>\n\n"
    t += "Select a plan to view details 👇" if rows else "<i>No plans available right now. Please check back soon.</i>"
    btns = []
    for p in rows:
        label = f"{p['name']} · {money(p['price'])} · {p['stk']} left" if p["stk"] else f"{p['name']} · Sold out"
        btns.append([B(label, f"p:{p['id']}", "success" if p["stk"] else None)])
    btns.append([B("Back", "home", None, "back")])
    await show(ev, t, kb(*btns))


async def screen_wallet(ev):
    uid = ev.from_user.id
    u = get_user(uid)
    n = sum(1 for o in D["orders"].values() if o["user_id"] == uid)
    t = (f"{E('wallet')} <b>My Wallet</b>\n\n{E('deposit')} Balance: <b>{money(u['balance'])}</b>\n"
         f"{E('cart')} Total spent: <b>{money(u['spent'])}</b>\n{E('orders')} Orders: <b>{n}</b>\n"
         f"{E('profile')} Member since: {fdate(u['joined_at'])}")
    await show(ev, t, kb([B("Add Funds", "dep", "success", "deposit")], [B("Back", "home", None, "back")]))


def user_orders(uid):
    return sorted((o for o in D["orders"].values() if o["user_id"] == uid), key=lambda o: -o["id"])[:10]


async def screen_orders(ev):
    rows = user_orders(ev.from_user.id)
    if not rows:
        return await show(ev, f"{E('orders')} <b>My Orders</b>\n\n<i>You have no orders yet.</i>",
                          kb([B("Buy Proxy", "cat:proxy", "primary", "proxy"), B("Buy VPN", "cat:vpn", "primary", "vpn")],
                             [B("Back", "home", None, "back")]))
    btns = [[B(f"#{o['id']} · {o['product_name']} · {money(o['price'])}", f"o:{o['id']}")] for o in rows]
    btns.append([B("Back", "home", None, "back")])
    await show(ev, f"{E('orders')} <b>My Orders</b> <i>(last 10)</i>", kb(*btns))


async def screen_notice(ev):
    n = SET.get("notice")
    await show(ev, f"{E('notice')} <b>Notice Board</b>\n\n" + (n or "<i>No notices right now.</i>"),
               kb([B("Back", "home", None, "back")]))


async def screen_support(ev):
    sup = SET.get("support", "").lstrip("@")
    if sup:
        return await show(ev, f"{E('support')} <b>Support</b>\n\nNeed help? Message us 👇",
                          kb([B("Contact Support", url=f"https://t.me/{sup}", style="primary", emoji="support")]))
    await show(ev, f"{E('support')} <b>Support</b>\n\nSupport contact has not been set yet.")


async def screen_dep(ev):
    methods = pay_methods()
    if not methods:
        return await show(ev, "Deposits are currently unavailable.")
    btns = [[B(n, f"depm:{i}", "primary", "wallet")] for i, (n, _) in enumerate(methods)]
    btns.append([B("Back", "home", None, "back")])
    await show(ev, f"{E('deposit')} <b>Add Funds</b>\n\nChoose a payment method 👇\n"
                   f"<i>Minimum deposit: {money(min_deposit())}</i>", kb(*btns))


def admin_kb():
    return kb(
        [B("Statistics", "adm:stats", "primary", "stats"), B("Deposits", "adm:deps", "success", "deposit")],
        [B("Products", "adm:prods", "primary", "stock"), B("Add Product", "adm:addp", "success", "add")],
        [B("Users", "adm:users", None, "users"), B("Broadcast", "adm:bc", "primary", "broadcast")],
        [B("Set Notice", "adm:notice", None, "notice"), B("Settings", "adm:set", None, "settings")],
        [B("Exit Admin", "home", "danger", "home")],
    )


ADMIN_BACK = kb([B("Admin Panel", "adm:home", None, "admin")])

# ═════════════ MENU ROUTER (bottom buttons — always wins over FSM states) ═════════════
menu = Router()


@menu.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(home_text(m.from_user.id), reply_markup=menu_kb(m.from_user.id))


@menu.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(home_text(m.from_user.id), reply_markup=menu_kb(m.from_user.id))


@menu.message(F.text.func(lambda t: menu_key(t) is not None))
async def on_menu(m: Message, state: FSMContext):
    await state.clear()
    k = menu_key(m.text)
    if k in ("proxy", "vpn"):
        await screen_cat(m, k)
    elif k == "deposit":
        await screen_dep(m)
    elif k == "wallet":
        await screen_wallet(m)
    elif k == "orders":
        await screen_orders(m)
    elif k == "notice":
        await screen_notice(m)
    elif k == "support":
        await screen_support(m)
    elif k == "admin" and m.from_user.id in ADMIN_IDS:
        await m.answer(f"{E('admin')} <b>Admin Panel</b>\n<i>Manage your store</i>", reply_markup=admin_kb())


# ═════════════ USER ROUTER ═════════════
router = Router()


class Dep(StatesGroup):
    amount = State()
    trx = State()


@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, home_text(c.from_user.id), home_kb(c.from_user.id))


@router.callback_query(F.data == "chk")
async def cb_chk(c: CallbackQuery):
    if FORCE_CHANNEL and not await joined(c.bot, c.from_user.id):
        return await c.answer("You haven't joined yet!", show_alert=True)
    await c.message.answer(home_text(c.from_user.id), reply_markup=menu_kb(c.from_user.id))
    await c.answer()


@router.callback_query(F.data.startswith("cat:"))
async def cb_cat(c: CallbackQuery):
    await screen_cat(c, c.data.split(":")[1])


@router.callback_query(F.data == "wallet")
async def cb_wallet(c: CallbackQuery):
    await screen_wallet(c)


@router.callback_query(F.data == "orders")
async def cb_orders(c: CallbackQuery):
    await screen_orders(c)


@router.callback_query(F.data == "notice")
async def cb_notice(c: CallbackQuery):
    await screen_notice(c)


@router.callback_query(F.data == "dep")
async def cb_dep(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await screen_dep(c)


@router.callback_query(F.data.startswith("p:"))
async def cb_product(c: CallbackQuery):
    p = product_row(int(c.data.split(":")[1]))
    if not p or not p["active"]:
        return await c.answer("Product not available.", show_alert=True)
    t = (f"{E(p['category'])} <b>{esc(p['name'])}</b>\n\n{esc(p.get('description') or '')}\n\n"
         f"{E('deposit')} <b>Price:</b> {money(p['price'])}\n{E('stock')} <b>In stock:</b> {p['stk']}\n"
         f"{E('bolt')} <b>Delivery:</b> Instant")
    rows = []
    if p["stk"]:
        rows.append([B("Buy Now", f"buy:{p['id']}", "success", "cart")])
    rows.append([B("Back", f"cat:{p['category']}", None, "back")])
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
        return await show(c, t, kb([B("Add Funds", "dep", "success", "deposit")],
                                   [B("Back", f"p:{p['id']}", None, "back")]))
    t = (f"{E('cart')} <b>Confirm purchase</b>\n\nPlan: <b>{esc(p['name'])}</b>\nPrice: <b>{money(p['price'])}</b>\n"
         f"Balance after: <b>{money(u['balance'] - p['price'])}</b>")
    await show(c, t, kb([B("Confirm", f"buyok:{p['id']}", "success", "ok"), B("Cancel", f"p:{p['id']}", "danger", "no")]))


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
            return await c.answer("Sorry, this plan just went out of stock.", show_alert=True)
        sid, item = min(items, key=lambda x: x[0])
        item["sold"] = 1
        u["balance"] -= p["price"]
        u["spent"] += p["price"]
        oid = await nid("orders")
        D["orders"][str(oid)] = {"id": oid, "user_id": uid, "product_id": pid, "product_name": p["name"],
                                 "price": p["price"], "data": item["data"], "created_at": int(time.time())}
        await put("stock", sid)
        await put("users", uid)
        await put("orders", oid)
    t = (f"{E('ok')} <b>Purchase successful!</b>\n\nOrder: <b>#{oid}</b>\nPlan: <b>{esc(p['name'])}</b>\n\n"
         f"{E('lock')} <b>Your details:</b>\n<code>{esc(item['data'])}</code>\n\n"
         f"<i>Tap the details to copy. Thank you for choosing {esc(BRAND)}!</i>")
    await show(c, t, kb([B("My Orders", "orders", None, "orders")], [B("Home", "home", "primary", "home")]))
    await notify_admins(c.bot, f"{E('cart')} <b>New order #{oid}</b>\nUser: <code>{uid}</code> "
                               f"(@{esc(c.from_user.username or '-')})\nPlan: {esc(p['name'])} · {money(p['price'])}")


@router.callback_query(F.data.startswith("o:"))
async def cb_order(c: CallbackQuery):
    o = D["orders"].get(c.data.split(":")[1])
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Order not found.", show_alert=True)
    t = (f"{E('orders')} <b>Order #{o['id']}</b>\n\nPlan: <b>{esc(o['product_name'])}</b>\nPrice: {money(o['price'])}\n"
         f"Date: {fdate(o['created_at'])}\n\n{E('lock')} <b>Details:</b>\n<code>{esc(o['data'])}</code>")
    await show(c, t, kb([B("Back", "orders", None, "back")]))


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
                  f"2️⃣ Now type the <b>amount</b> you sent (number only, min {money(min_deposit())}).",
               kb([B("Cancel", "home", "danger", "no")]))


@router.message(StateFilter(Dep.amount))
async def dep_amount(m: Message, state: FSMContext):
    try:
        amt = float((m.text or "").replace(",", "").strip())
    except ValueError:
        return await m.answer("❗ Please send a valid number, e.g. <code>5</code>")
    if amt < min_deposit():
        return await m.answer(f"❗ Minimum deposit is <b>{money(min_deposit())}</b>")
    await state.update_data(amount=amt)
    await state.set_state(Dep.trx)
    await m.answer(f"{E('ok')} Amount: <b>{money(amt)}</b>\n\nNow send your <b>Transaction ID / reference</b>:",
                   reply_markup=kb([B("Cancel", "home", "danger", "no")]))


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
                   f"⏳ Our team will verify it shortly. You'll be notified once approved.",
                   reply_markup=kb([B("Home", "home", "primary", "home")]))
    await notify_admins(
        m.bot, f"{E('deposit')} <b>New deposit #{did}</b>\nUser: <code>{m.from_user.id}</code> "
               f"(@{esc(m.from_user.username or '-')})\nMethod: {esc(d['method'])}\n"
               f"Amount: <b>{money(d['amount'])}</b>\nTrx: <code>{esc(trx)}</code>",
        kb([B("Approve", f"adm:dok:{did}", "success", "ok"), B("Reject", f"adm:dno:{did}", "danger", "no")]))


# ═════════════ ADMIN ROUTER ═════════════
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class AddP(StatesGroup):
    name = State(); desc = State(); price = State()
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


@admin.message(Command("admin"))
async def adm_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(f"{E('admin')} <b>Admin Panel</b>", reply_markup=admin_kb())


@admin.callback_query(F.data == "adm:home")
async def adm_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, f"{E('admin')} <b>Admin Panel</b>\n<i>Manage your store</i>", admin_kb())


@admin.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    day = int(time.time()) - 86400
    users, orders, deps = D["users"].values(), D["orders"].values(), D["deposits"].values()
    t = (f"{E('stats')} <b>Statistics</b>\n\n"
         f"{E('users')} Users: <b>{len(D['users'])}</b> (+{sum(1 for u in users if u['joined_at'] > day)} today)\n"
         f"{E('stock')} Products: <b>{len(D['products'])}</b> · Stock left: <b>{sum(1 for s in D['stock'].values() if not s['sold'])}</b>\n"
         f"{E('orders')} Orders: <b>{len(D['orders'])}</b>\n"
         f"{E('cart')} Revenue: <b>{money(sum(o['price'] for o in orders))}</b> "
         f"(24h: {money(sum(o['price'] for o in orders if o['created_at'] > day))})\n"
         f"{E('deposit')} Deposits approved: <b>{money(sum(d['amount'] for d in deps if d['status'] == 'approved'))}</b>\n"
         f"⏳ Pending deposits: <b>{sum(1 for d in deps if d['status'] == 'pending')}</b>\n"
         f"{E('wallet')} Users' total balance: <b>{money(sum(u['balance'] for u in users))}</b>")
    await show(c, t, kb([B("Refresh", "adm:stats", "primary", "bolt")], [B("Admin Panel", "adm:home", None, "admin")]))


# ── Products ──
@admin.callback_query(F.data == "adm:addp")
async def adm_addp(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, f"{E('add')} <b>Add Product</b>\n\nChoose a category:",
               kb([B("Proxy", "adm:addp:proxy", "primary", "proxy"), B("VPN", "adm:addp:vpn", "primary", "vpn")],
                  [B("Cancel", "adm:home", "danger", "no")]))


@admin.callback_query(F.data.startswith("adm:addp:"))
async def adm_addp_cat(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddP.name)
    await state.update_data(cat=c.data.split(":")[2])
    await show(c, "✏️ Send the <b>product name</b>\n<i>e.g. USA Residential Proxy – 30 Days</i>",
               kb([B("Cancel", "adm:home", "danger", "no")]))


@admin.message(StateFilter(AddP.name))
async def addp_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(AddP.desc)
    await m.answer("📝 Send the <b>description</b> (features, speed, locations…)")


@admin.message(StateFilter(AddP.desc))
async def addp_desc(m: Message, state: FSMContext):
    await state.update_data(desc=m.text.strip())
    await state.set_state(AddP.price)
    await m.answer(f"{E('deposit')} Send the <b>price</b> (number, in {CURRENCY})")


@admin.message(StateFilter(AddP.price))
async def addp_price(m: Message, state: FSMContext):
    try:
        price = float(m.text.replace(",", "").strip())
    except ValueError:
        return await m.answer("❗ Send a valid number.")
    d = await state.get_data()
    pid = await nid("products")
    D["products"][str(pid)] = {"id": pid, "category": d["cat"], "name": d["name"],
                               "description": d["desc"], "price": price, "active": 1}
    await put("products", pid)
    await state.clear()
    await m.answer(f"{E('ok')} Product created!", reply_markup=kb(
        [B("Add Stock", f"adm:stk:{pid}", "success", "stock")], [B("Admin Panel", "adm:home", None, "admin")]))


@admin.callback_query(F.data == "adm:prods")
async def adm_prods(c: CallbackQuery):
    rows = sorted((product_row(p["id"]) for p in D["products"].values()), key=lambda p: -p["id"])[:40]
    btns = [[B(f"{'🟢' if p['active'] else '🔴'} #{p['id']} {p['name']} · {money(p['price'])} · {p['stk']}",
               f"adm:p:{p['id']}")] for p in rows]
    btns += [[B("Add Product", "adm:addp", "success", "add")], [B("Admin Panel", "adm:home", None, "admin")]]
    await show(c, f"{E('stock')} <b>Products</b> <i>(🟢 active · 🔴 hidden · stock)</i>", kb(*btns))


async def render_admin_product(c: CallbackQuery, pid: int):
    p = product_row(pid)
    if not p:
        return await c.answer("Not found", show_alert=True)
    sold = sum(1 for s in D["stock"].values() if int(s["pid"]) == pid and s["sold"])
    t = (f"{E(p['category'])} <b>#{p['id']} {esc(p['name'])}</b>\n\n{esc(p.get('description') or '')}\n\n"
         f"Price: <b>{money(p['price'])}</b>\nIn stock: <b>{p['stk']}</b> · Sold: <b>{sold}</b>\n"
         f"Status: {'🟢 Active' if p['active'] else '🔴 Hidden'}")
    await show(c, t, kb(
        [B("Add Stock", f"adm:stk:{pid}", "success", "stock"), B("Edit Price", f"adm:ep:{pid}", "primary", "edit")],
        [B("Hide" if p["active"] else "Show", f"adm:tg:{pid}", None, "lock"), B("Delete", f"adm:del:{pid}", "danger", "trash")],
        [B("Back", "adm:prods", None, "back")]))


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
               kb([B("Yes, delete", f"adm:delok:{pid}", "danger", "trash"), B("No", f"adm:p:{pid}", "success", "no")]))


@admin.callback_query(F.data.startswith("adm:delok:"))
async def adm_delok(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    dead = [k for k, s in D["stock"].items() if int(s["pid"]) == pid and not s["sold"]]
    for k in dead:
        del D["stock"][k]
    if dead:
        await put("stock", *dead)
    D["products"].pop(str(pid), None)
    await put("products", pid)
    await c.answer("Deleted ✅")
    await adm_prods(c)


@admin.callback_query(F.data.startswith("adm:ep:"))
async def adm_ep(c: CallbackQuery, state: FSMContext):
    await state.set_state(EditPrice.price)
    await state.update_data(pid=int(c.data.split(":")[2]))
    await show(c, "✏️ Send the <b>new price</b>:", kb([B("Cancel", "adm:home", "danger", "no")]))


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
                   reply_markup=kb([B("Back to product", f"adm:p:{d['pid']}", None, "back")]))


@admin.callback_query(F.data.startswith("adm:stk:"))
async def adm_stk(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddStock.lines)
    await state.update_data(pid=int(c.data.split(":")[2]))
    await show(c, f"{E('stock')} <b>Add Stock</b>\n\nSend items — <b>one per line</b>.\n"
                  "For multi-line items, separate them with a line containing only <code>---</code>.\n\n"
                  "<i>Example:</i>\n<code>1.2.3.4:8080:user:pass\n5.6.7.8:8080:user:pass</code>",
               kb([B("Cancel", "adm:home", "danger", "no")]))


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
                   reply_markup=kb([B("Back to product", f"adm:p:{d['pid']}", None, "back")],
                                   [B("Admin Panel", "adm:home", None, "admin")]))


# ── Deposits ──
@admin.callback_query(F.data == "adm:deps")
async def adm_deps(c: CallbackQuery):
    rows = sorted((d for d in D["deposits"].values() if d["status"] == "pending"), key=lambda d: d["id"])[:20]
    if not rows:
        return await show(c, f"{E('deposit')} <b>Pending Deposits</b>\n\n<i>Nothing pending 🎉</i>", ADMIN_BACK)
    btns = [[B(f"#{d['id']} · {money(d['amount'])} · {d['method']} · {d['user_id']}", f"adm:d:{d['id']}", "primary")]
            for d in rows]
    btns.append([B("Admin Panel", "adm:home", None, "admin")])
    await show(c, f"{E('deposit')} <b>Pending Deposits</b>", kb(*btns))


@admin.callback_query(F.data.startswith("adm:d:"))
async def adm_dep(c: CallbackQuery):
    d = D["deposits"].get(c.data.split(":")[2])
    if not d:
        return await c.answer("Not found", show_alert=True)
    t = (f"{E('deposit')} <b>Deposit #{d['id']}</b>\nUser: <code>{d['user_id']}</code>\nMethod: {esc(d['method'])}\n"
         f"Amount: <b>{money(d['amount'])}</b>\nTrx: <code>{esc(d['trx'])}</code>\nStatus: <b>{d['status']}</b>\n"
         f"Date: {fdate(d['created_at'])}")
    rows = []
    if d["status"] == "pending":
        rows.append([B("Approve", f"adm:dok:{d['id']}", "success", "ok"), B("Reject", f"adm:dno:{d['id']}", "danger", "no")])
    rows.append([B("Back", "adm:deps", None, "back")])
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
            d["user_id"], f"{E('ok')} <b>Deposit approved!</b>\n\n+{money(d['amount'])} added to your wallet.\n"
                          f"New balance: <b>{money(u['balance'])}</b>",
            reply_markup=kb([B("Buy Proxy", "cat:proxy", "primary", "proxy"), B("Buy VPN", "cat:vpn", "primary", "vpn")]))
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


# ── Users ──
@admin.callback_query(F.data == "adm:users")
async def adm_users(c: CallbackQuery, state: FSMContext):
    await state.set_state(FindUser.query)
    await show(c, f"{E('users')} <b>Users</b>\n\nSend a <b>user ID</b> or <b>@username</b> to manage:",
               kb([B("Cancel", "adm:home", "danger", "no")]))


def user_panel(uid: int):
    u = get_user(uid)
    if not u:
        return None, None
    n = sum(1 for o in D["orders"].values() if o["user_id"] == uid)
    t = (f"{E('profile')} <b>{esc(u.get('full_name') or '-')}</b> (@{esc(u.get('username') or '-')})\n"
         f"ID: <code>{uid}</code>\nBalance: <b>{money(u['balance'])}</b>\nSpent: {money(u['spent'])}\n"
         f"Orders: {n}\nJoined: {fdate(u['joined_at'])}\nStatus: {'🚫 Banned' if u['banned'] else '✅ Active'}")
    return t, kb(
        [B("Add Balance", f"adm:adj:{uid}:+", "success", "add"), B("Deduct", f"adm:adj:{uid}:-", "danger", "no")],
        [B("Unban" if u["banned"] else "Ban", f"adm:ban:{uid}", "success" if u["banned"] else "danger", "ban")],
        [B("Admin Panel", "adm:home", None, "admin")])


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


@admin.callback_query(F.data.startswith("adm:adj:"))
async def adm_adj(c: CallbackQuery, state: FSMContext):
    _, _, uid, sign = c.data.split(":")
    await state.set_state(Adjust.amount)
    await state.update_data(uid=int(uid), sign=sign)
    await show(c, f"Send the amount to <b>{'add to' if sign == '+' else 'deduct from'}</b> the balance:",
               kb([B("Cancel", "adm:home", "danger", "no")]))


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
@admin.callback_query(F.data == "adm:bc")
async def adm_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.msg)
    await show(c, f"{E('broadcast')} <b>Broadcast</b>\n\nSend the message to deliver to <b>all users</b>.\n"
                  "<i>Text, photo, video, custom emoji — copied as is.</i>",
               kb([B("Cancel", "adm:home", "danger", "no")]))


@admin.message(StateFilter(Broadcast.msg))
async def bc_preview(m: Message, state: FSMContext):
    n = sum(1 for u in D["users"].values() if not u["banned"])
    await state.update_data(chat=m.chat.id, mid=m.message_id)
    await m.answer(f"{E('broadcast')} Ready to send to <b>{n}</b> users.\nPreview is the message above ☝️",
                   reply_markup=kb([B(f"Send to {n} users", "adm:bcgo", "success", "broadcast")],
                                   [B("Cancel", "adm:home", "danger", "no")]))


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
    await c.message.answer(f"{E('ok')} <b>Broadcast finished</b>\n\nDelivered: <b>{ok}</b>\nFailed: <b>{fail}</b>",
                           reply_markup=ADMIN_BACK)


@admin.callback_query(F.data == "adm:notice")
async def adm_notice(c: CallbackQuery, state: FSMContext):
    await state.set_state(NoticeS.text)
    await show(c, f"{E('notice')} <b>Notice Board</b>\n\nCurrent:\n{SET.get('notice') or '<i>(none)</i>'}\n\n"
                  "Send the new notice (formatting &amp; custom emoji supported).\nSend <code>clear</code> to remove it.",
               kb([B("Cancel", "adm:home", "danger", "no")]))


@admin.message(StateFilter(NoticeS.text))
async def notice_save(m: Message, state: FSMContext):
    await state.clear()
    if (m.text or "").strip().lower() == "clear":
        await del_setting("notice")
        return await m.answer(f"{E('ok')} Notice cleared.", reply_markup=ADMIN_BACK)
    await set_setting("notice", m.html_text)
    await m.answer(f"{E('ok')} Notice updated!", reply_markup=kb(
        [B("Also broadcast it", "adm:bc", "primary", "broadcast")], [B("Admin Panel", "adm:home", None, "admin")]))


# ── Settings ──
SETTING_PROMPTS = {
    "support": "Send the <b>support username</b> (e.g. <code>@MySupport</code>). Send <code>clear</code> to remove.",
    "payments": "Send payment methods — <b>one per line</b> as <code>Name|Details</code>:\n"
                "<code>bKash|017XXXXXXXX (Send Money)\nUSDT TRC20|Txxxxxxxx</code>",
    "min_dep": f"Send the <b>minimum deposit</b> amount (number, {CURRENCY}).",
}


@admin.callback_query(F.data == "adm:set")
async def adm_set(c: CallbackQuery, state: FSMContext):
    await state.clear()
    mnt = SET.get("maintenance") == "1"
    t = (f"{E('settings')} <b>Settings</b>\n\nSupport: <b>{esc(SET.get('support') or '-')}</b>\n"
         f"Min deposit: <b>{money(min_deposit())}</b>\nPayment methods: <b>{len(pay_methods())}</b>\n"
         f"Maintenance: <b>{'ON 🔴' if mnt else 'OFF 🟢'}</b>")
    await show(c, t, kb(
        [B("Support", "adm:sv:support", "primary", "support"), B("Min Deposit", "adm:sv:min_dep", "primary", "deposit")],
        [B("Payment Methods", "adm:sv:payments", "success", "wallet")],
        [B("Emoji IDs (Buttons)", "adm:emo", "primary", "star")],
        [B("Maintenance: ON" if mnt else "Maintenance: OFF", "adm:mnt", "danger" if mnt else None, "settings")],
        [B("Admin Panel", "adm:home", None, "admin")]))


@admin.callback_query(F.data == "adm:mnt")
async def adm_mnt(c: CallbackQuery, state: FSMContext):
    await set_setting("maintenance", "0" if SET.get("maintenance") == "1" else "1")
    await adm_set(c, state)


@admin.callback_query(F.data.startswith("adm:sv:"))
async def adm_sv(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.set_state(SetVal.value)
    await state.update_data(key=key)
    await show(c, SETTING_PROMPTS[key], kb([B("Cancel", "adm:set", "danger", "no")]))


@admin.message(StateFilter(SetVal.value))
async def sv_save(m: Message, state: FSMContext):
    d = await state.get_data()
    key, val = d["key"], (m.text or "").strip()
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
    await m.answer(f"{E('ok')} Saved.", reply_markup=kb([B("Settings", "adm:set", None, "settings")]))


# ── Custom emoji IDs (buttons + bottom menu + messages) ──
@admin.callback_query(F.data == "adm:emo")
async def adm_emo(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows, row = [], []
    for k in EMOJI:
        row.append(B(f"{EMOJI[k]} {k} {'✅' if emoji_id(k) else '·'}", f"adm:emk:{k}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("Settings", "adm:set", None, "settings")])
    await show(c, f"{E('star')} <b>Custom Emoji IDs</b>\n\nPick a slot, then <b>send one custom emoji</b> "
                  "(from any Premium pack) — the bot reads its ID automatically. You can also send a numeric ID, "
                  "or <code>clear</code>.\n\n✅ = custom emoji set\n"
                  "<i>Used on inline buttons, bottom menu buttons and inside messages.\n"
                  "Send /start after changing to refresh the bottom menu.</i>", kb(*rows))


@admin.callback_query(F.data.startswith("adm:emk:"))
async def adm_emk(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.set_state(EmojiS.value)
    await state.update_data(key=key)
    await show(c, f"{EMOJI[key]} <b>{key}</b>\nCurrent ID: <code>{emoji_id(key) or 'none'}</code>\n\n"
                  "Send a <b>custom emoji</b> now (or its numeric ID / <code>clear</code>).",
               kb([B("Cancel", "adm:emo", "danger", "no")]))


@admin.message(StateFilter(EmojiS.value))
async def emo_save(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    cid = next((e.custom_emoji_id for e in (m.entities or []) if e.type == "custom_emoji"), None)
    txt = (m.text or "").strip()
    if txt.lower() == "clear":
        await del_setting(f"emoji:{key}")
        await state.clear()
        return await m.answer(f"{E('ok')} Cleared <b>{key}</b>.", reply_markup=kb([B("Emoji list", "adm:emo", None, "star")]))
    if not cid and txt.isdigit():
        cid = txt
    if not cid:
        return await m.answer("❗ I couldn't find a custom emoji. Send a <b>custom emoji</b> or a numeric ID.")
    await set_setting(f"emoji:{key}", cid)
    await state.clear()
    await m.answer(f"{E('ok')} <b>{key}</b> → <code>{cid}</code>\nPreview: {E(key)}",
                   reply_markup=menu_kb(m.from_user.id))
    await m.answer("Saved. Open the list to set more:", reply_markup=kb(
        [B("Test button", "adm:emo", "success", key), B("Emoji list", "adm:emo", None, "star")]))


# ═════════════ MAIN ═════════════
async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN is missing. Copy .env.example to .env and fill it in.")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty — nobody can open the admin panel!")
    await asyncio.to_thread(init_store)

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