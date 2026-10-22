# -*- coding: utf-8 -*-
"""
Premium Telegram Proxy & VPN Shop Bot
-------------------------------------
* Colored inline buttons  (style: primary = blue, success = green, danger = red)
* Custom emoji on buttons (icon_custom_emoji_id) + in messages (<tg-emoji>)
* Wallet system with manual deposits (admin approve / reject)
* Instant delivery from stock
* Full admin panel (products, stock, deposits, users, broadcast, notice, settings)

Requires: aiogram >= 3.22, aiosqlite, python-dotenv
"""
import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
BRAND = os.getenv("BRAND_NAME", "PremiumNet")
CURRENCY = os.getenv("CURRENCY", "$")
DB_PATH = os.getenv("DB_PATH", "shop.db")
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "").strip()

# Fallback emoji (shown when no custom emoji ID is set)
EMOJI = {
    "fire": "🔥", "proxy": "🌐", "vpn": "🔐", "wallet": "💳", "orders": "📦",
    "support": "💬", "notice": "📢", "back": "⬅️", "admin": "👑", "stats": "📊",
    "add": "➕", "broadcast": "📣", "users": "👥", "deposit": "💰",
    "settings": "⚙️", "ok": "✅", "no": "❌", "home": "🏠", "profile": "👤",
    "star": "⭐", "lock": "🔒", "stock": "🗃", "edit": "✏️", "trash": "🗑",
    "ban": "🚫", "bolt": "⚡", "gift": "🎁", "cart": "🛒",
}

# You can paste default custom emoji IDs here, e.g. "proxy": "5368324170671202286"
# (or set them live from  Admin Panel → Settings → Emoji IDs)
DEFAULT_EMOJI_IDS: dict[str, str] = {
    # "fire": "5368324170671202286",
}

DEFAULT_PAYMENTS = (
    "bKash|01XXXXXXXXX (Send Money)\n"
    "Nagad|01XXXXXXXXX (Send Money)\n"
    "USDT TRC20|TXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("shop")

# ════════════════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════════════════
db: aiosqlite.Connection
SET: dict[str, str] = {}          # settings cache
buy_lock = asyncio.Lock()


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            balance REAL DEFAULT 0, spent REAL DEFAULT 0,
            banned INTEGER DEFAULT 0, joined_at INTEGER);
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, name TEXT,
            description TEXT, price REAL, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS stock(
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER,
            data TEXT, sold INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            product_id INTEGER, product_name TEXT, price REAL,
            data TEXT, created_at INTEGER);
        CREATE TABLE IF NOT EXISTS deposits(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            method TEXT, amount REAL, trx TEXT,
            status TEXT DEFAULT 'pending', created_at INTEGER);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """
    )
    await db.commit()
    for r in await q_all("SELECT key, value FROM settings"):
        SET[r["key"]] = r["value"]


async def q_one(sql, *a):
    async with db.execute(sql, a) as cur:
        return await cur.fetchone()


async def q_all(sql, *a):
    async with db.execute(sql, a) as cur:
        return await cur.fetchall()


async def q_run(sql, *a):
    cur = await db.execute(sql, a)
    await db.commit()
    return cur


async def set_setting(key: str, value: str):
    SET[key] = value
    await q_run("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", key, value)


async def del_setting(key: str):
    SET.pop(key, None)
    await q_run("DELETE FROM settings WHERE key=?", key)


# ════════════════════════════════════════════════════════════════════
#  HELPERS / UI
# ════════════════════════════════════════════════════════════════════
esc = html.escape


def money(x) -> str:
    return f"{CURRENCY}{float(x):,.2f}"


def fdate(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %b %Y, %H:%M")


def emoji_id(key: str) -> str:
    return SET.get(f"emoji:{key}") or DEFAULT_EMOJI_IDS.get(key, "")


def E(key: str) -> str:
    """Emoji for message text (custom emoji if configured)."""
    cid = emoji_id(key)
    fb = EMOJI.get(key, "•")
    return f'<tg-emoji emoji-id="{cid}">{fb}</tg-emoji>' if cid else fb


def B(text, cb=None, style=None, emoji=None, url=None) -> InlineKeyboardButton:
    """
    Pretty button.
      style : 'primary' (blue) | 'success' (green) | 'danger' (red) | None
      emoji : key from EMOJI -> uses custom emoji icon if ID is set
    """
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


async def show(ev, text, markup=None):
    """Edit the message if it's a callback, otherwise send a new one."""
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
    raw = SET.get("payments") or DEFAULT_PAYMENTS
    out = []
    for line in raw.splitlines():
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


async def get_user(uid: int):
    return await q_one("SELECT * FROM users WHERE id=?", uid)


async def touch_user(u):
    row = await get_user(u.id)
    name = u.full_name or ""
    if row is None:
        await q_run(
            "INSERT INTO users(id,username,full_name,joined_at) VALUES(?,?,?,?)",
            u.id, u.username, name, int(time.time()),
        )
        row = await get_user(u.id)
    elif row["username"] != u.username or row["full_name"] != name:
        await q_run("UPDATE users SET username=?, full_name=? WHERE id=?", u.username, name, u.id)
    return row


async def joined(bot: Bot, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(FORCE_CHANNEL, uid)
        return str(getattr(m.status, "value", m.status)) in ("member", "administrator", "creator")
    except Exception:
        return True  # fail-open


# ════════════════════════════════════════════════════════════════════
#  KEYBOARDS / TEXTS (USER)
# ════════════════════════════════════════════════════════════════════
async def home_text(uid: int) -> str:
    u = await get_user(uid)
    t = (
        f"{E('fire')} <b>Welcome to {esc(BRAND)}</b>\n"
        f"<i>Premium Proxy &amp; VPN · Instant delivery · 24/7</i>\n\n"
        f"{E('profile')} <b>ID:</b> <code>{uid}</code>\n"
        f"{E('wallet')} <b>Balance:</b> {money(u['balance'])}\n"
    )
    notice = SET.get("notice")
    if notice:
        t += f"\n{E('notice')} <b>Notice</b>\n{notice}\n"
    t += "\n<i>Choose an option below</i> 👇"
    return t


def home_kb(uid: int) -> InlineKeyboardMarkup:
    sup = SET.get("support", "").lstrip("@")
    support_btn = (
        B("Support", url=f"https://t.me/{sup}", emoji="support")
        if sup else B("Support", "support", emoji="support")
    )
    rows = [
        [B("Buy Proxy", "cat:proxy", "primary", "proxy"), B("Buy VPN", "cat:vpn", "primary", "vpn")],
        [B("Add Funds", "dep", "success", "deposit"), B("My Wallet", "wallet", None, "wallet")],
        [B("My Orders", "orders", None, "orders"), B("Notice", "notice", None, "notice")],
        [support_btn],
    ]
    if uid in ADMIN_IDS:
        rows.append([B("Admin Panel", "adm:home", "danger", "admin")])
    return kb(*rows)


# ════════════════════════════════════════════════════════════════════
#  MIDDLEWARE (ban / maintenance / force-join)
# ════════════════════════════════════════════════════════════════════
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
            return await deny(f"{E('settings')} <b>Maintenance mode</b>\nWe'll be back shortly. Thank you for your patience!")
        if FORCE_CHANNEL and not (isinstance(event, CallbackQuery) and event.data == "chk"):
            if not await joined(data["bot"], u.id):
                url = f"https://t.me/{FORCE_CHANNEL.lstrip('@')}"
                return await deny(
                    f"{E('lock')} <b>Please join our channel first</b>\nJoin, then tap <b>I've Joined</b>.",
                    kb([B("Join Channel", url=url, style="primary", emoji="notice")],
                       [B("I've Joined", "chk", "success", "ok")]),
                )
        return await handler(event, data)


# ════════════════════════════════════════════════════════════════════
#  USER ROUTER
# ════════════════════════════════════════════════════════════════════
router = Router()


class Dep(StatesGroup):
    amount = State()
    trx = State()


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await home_text(m.from_user.id), reply_markup=home_kb(m.from_user.id))


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await home_text(m.from_user.id), reply_markup=home_kb(m.from_user.id))


@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, await home_text(c.from_user.id), home_kb(c.from_user.id))


@router.callback_query(F.data == "chk")
async def cb_chk(c: CallbackQuery):
    if FORCE_CHANNEL and not await joined(c.bot, c.from_user.id):
        return await c.answer("You haven't joined yet!", show_alert=True)
    await show(c, await home_text(c.from_user.id), home_kb(c.from_user.id))


@router.callback_query(F.data == "notice")
async def cb_notice(c: CallbackQuery):
    n = SET.get("notice")
    t = f"{E('notice')} <b>Notice Board</b>\n\n" + (n if n else "<i>No notices right now.</i>")
    await show(c, t, kb([B("Back", "home", None, "back")]))


@router.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    await show(c, f"{E('support')} <b>Support</b>\n\nSupport contact has not been set yet.",
               kb([B("Back", "home", None, "back")]))


# ── Catalog ─────────────────────────────────────────────────────────
CAT_NAMES = {"proxy": "Proxy", "vpn": "VPN"}


@router.callback_query(F.data.startswith("cat:"))
async def cb_cat(c: CallbackQuery):
    cat = c.data.split(":")[1]
    rows = await q_all(
        """SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id AND s.sold=0) AS stk
           FROM products p WHERE p.category=? AND p.active=1 ORDER BY p.id""", cat)
    t = f"{E(cat)} <b>{CAT_NAMES.get(cat, cat)} Plans</b>\n\n"
    if not rows:
        t += "<i>No plans available right now. Please check back soon.</i>"
    else:
        t += "Select a plan to view details 👇"
    btns = []
    for p in rows:
        label = f"{p['name']} · {money(p['price'])} · {p['stk']} left" if p["stk"] else f"{p['name']} · Sold out"
        btns.append([B(label, f"p:{p['id']}", "success" if p["stk"] else None)])
    btns.append([B("Back", "home", None, "back")])
    await show(c, t, kb(*btns))


async def product_row(pid: int):
    return await q_one(
        """SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id AND s.sold=0) AS stk
           FROM products p WHERE p.id=?""", pid)


@router.callback_query(F.data.startswith("p:"))
async def cb_product(c: CallbackQuery):
    p = await product_row(int(c.data.split(":")[1]))
    if not p or not p["active"]:
        return await c.answer("Product not available.", show_alert=True)
    t = (
        f"{E(p['category'])} <b>{esc(p['name'])}</b>\n\n"
        f"{esc(p['description'] or '')}\n\n"
        f"{E('deposit')} <b>Price:</b> {money(p['price'])}\n"
        f"{E('stock')} <b>In stock:</b> {p['stk']}\n"
        f"{E('bolt')} <b>Delivery:</b> Instant"
    )
    rows = []
    if p["stk"]:
        rows.append([B("Buy Now", f"buy:{p['id']}", "success", "cart")])
    rows.append([B("Back", f"cat:{p['category']}", None, "back")])
    await show(c, t, kb(*rows))


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery):
    p = await product_row(int(c.data.split(":")[1]))
    u = await get_user(c.from_user.id)
    if not p or not p["active"] or not p["stk"]:
        return await c.answer("Out of stock.", show_alert=True)
    if u["balance"] < p["price"]:
        t = (f"{E('no')} <b>Insufficient balance</b>\n\n"
             f"Price: {money(p['price'])}\nYour balance: {money(u['balance'])}\n\n"
             f"Please add funds first.")
        return await show(c, t, kb([B("Add Funds", "dep", "success", "deposit")],
                                   [B("Back", f"p:{p['id']}", None, "back")]))
    t = (f"{E('cart')} <b>Confirm purchase</b>\n\n"
         f"Plan: <b>{esc(p['name'])}</b>\nPrice: <b>{money(p['price'])}</b>\n"
         f"Balance after: <b>{money(u['balance'] - p['price'])}</b>")
    await show(c, t, kb([B("Confirm", f"buyok:{p['id']}", "success", "ok"),
                         B("Cancel", f"p:{p['id']}", "danger", "no")]))


@router.callback_query(F.data.startswith("buyok:"))
async def cb_buyok(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    uid = c.from_user.id
    async with buy_lock:
        p = await q_one("SELECT * FROM products WHERE id=? AND active=1", pid)
        u = await get_user(uid)
        if not p:
            return await c.answer("Product not available.", show_alert=True)
        if u["balance"] < p["price"]:
            return await c.answer("Insufficient balance.", show_alert=True)
        item = await q_one("SELECT * FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT 1", pid)
        if not item:
            return await c.answer("Sorry, this plan just went out of stock.", show_alert=True)
        await db.execute("UPDATE stock SET sold=1 WHERE id=?", (item["id"],))
        await db.execute("UPDATE users SET balance=balance-?, spent=spent+? WHERE id=?", (p["price"], p["price"], uid))
        cur = await db.execute(
            "INSERT INTO orders(user_id,product_id,product_name,price,data,created_at) VALUES(?,?,?,?,?,?)",
            (uid, pid, p["name"], p["price"], item["data"], int(time.time())))
        await db.commit()
        oid = cur.lastrowid
    t = (f"{E('ok')} <b>Purchase successful!</b>\n\n"
         f"Order: <b>#{oid}</b>\nPlan: <b>{esc(p['name'])}</b>\n\n"
         f"{E('lock')} <b>Your details:</b>\n<code>{esc(item['data'])}</code>\n\n"
         f"<i>Tap the details to copy. Thank you for choosing {esc(BRAND)}!</i>")
    await show(c, t, kb([B("My Orders", "orders", None, "orders")], [B("Home", "home", "primary", "home")]))
    await notify_admins(
        c.bot,
        f"{E('cart')} <b>New order #{oid}</b>\nUser: <code>{uid}</code> (@{esc(c.from_user.username or '-')})\n"
        f"Plan: {esc(p['name'])} · {money(p['price'])}")


# ── Wallet / Orders ─────────────────────────────────────────────────
@router.callback_query(F.data == "wallet")
async def cb_wallet(c: CallbackQuery):
    u = await get_user(c.from_user.id)
    n = (await q_one("SELECT COUNT(*) n FROM orders WHERE user_id=?", c.from_user.id))["n"]
    t = (f"{E('wallet')} <b>My Wallet</b>\n\n"
         f"{E('deposit')} Balance: <b>{money(u['balance'])}</b>\n"
         f"{E('cart')} Total spent: <b>{money(u['spent'])}</b>\n"
         f"{E('orders')} Orders: <b>{n}</b>\n"
         f"{E('profile')} Member since: {fdate(u['joined_at'])}")
    await show(c, t, kb([B("Add Funds", "dep", "success", "deposit")], [B("Back", "home", None, "back")]))


@router.callback_query(F.data == "orders")
async def cb_orders(c: CallbackQuery):
    rows = await q_all("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", c.from_user.id)
    if not rows:
        t = f"{E('orders')} <b>My Orders</b>\n\n<i>You have no orders yet.</i>"
        return await show(c, t, kb([B("Buy Proxy", "cat:proxy", "primary", "proxy"),
                                    B("Buy VPN", "cat:vpn", "primary", "vpn")],
                                   [B("Back", "home", None, "back")]))
    btns = [[B(f"#{o['id']} · {o['product_name']} · {money(o['price'])}", f"o:{o['id']}")] for o in rows]
    btns.append([B("Back", "home", None, "back")])
    await show(c, f"{E('orders')} <b>My Orders</b> <i>(last 10)</i>", kb(*btns))


@router.callback_query(F.data.startswith("o:"))
async def cb_order(c: CallbackQuery):
    o = await q_one("SELECT * FROM orders WHERE id=? AND user_id=?", int(c.data.split(":")[1]), c.from_user.id)
    if not o:
        return await c.answer("Order not found.", show_alert=True)
    t = (f"{E('orders')} <b>Order #{o['id']}</b>\n\nPlan: <b>{esc(o['product_name'])}</b>\n"
         f"Price: {money(o['price'])}\nDate: {fdate(o['created_at'])}\n\n"
         f"{E('lock')} <b>Details:</b>\n<code>{esc(o['data'])}</code>")
    await show(c, t, kb([B("Back", "orders", None, "back")]))


# ── Deposit flow ────────────────────────────────────────────────────
@router.callback_query(F.data == "dep")
async def cb_dep(c: CallbackQuery, state: FSMContext):
    await state.clear()
    methods = pay_methods()
    if not methods:
        return await c.answer("Deposits are currently unavailable.", show_alert=True)
    btns = [[B(n, f"depm:{i}", "primary", "wallet")] for i, (n, _) in enumerate(methods)]
    btns.append([B("Back", "home", None, "back")])
    await show(c, f"{E('deposit')} <b>Add Funds</b>\n\nChoose a payment method 👇\n"
                  f"<i>Minimum deposit: {money(min_deposit())}</i>", kb(*btns))


@router.callback_query(F.data.startswith("depm:"))
async def cb_depm(c: CallbackQuery, state: FSMContext):
    idx = int(c.data.split(":")[1])
    methods = pay_methods()
    if idx >= len(methods):
        return await c.answer("Method not found.", show_alert=True)
    name, details = methods[idx]
    await state.set_state(Dep.amount)
    await state.update_data(method=name)
    t = (f"{E('wallet')} <b>{esc(name)}</b>\n\n<code>{esc(details)}</code>\n\n"
         f"1️⃣ Send the money to the above account.\n"
         f"2️⃣ Now type the <b>amount</b> you sent (number only, min {money(min_deposit())}).")
    await show(c, t, kb([B("Cancel", "home", "danger", "no")]))


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
    d = await state.get_data()
    trx = (m.text or "").strip()
    if len(trx) < 4:
        return await m.answer("❗ Please send a valid Transaction ID.")
    cur = await q_run("INSERT INTO deposits(user_id,method,amount,trx,created_at) VALUES(?,?,?,?,?)",
                      m.from_user.id, d["method"], d["amount"], trx, int(time.time()))
    did = cur.lastrowid
    await state.clear()
    await m.answer(
        f"{E('ok')} <b>Deposit request submitted!</b>\n\nRequest: <b>#{did}</b>\nMethod: {esc(d['method'])}\n"
        f"Amount: {money(d['amount'])}\nTrx: <code>{esc(trx)}</code>\n\n"
        f"⏳ Our team will verify it shortly. You'll be notified once approved.",
        reply_markup=kb([B("Home", "home", "primary", "home")]))
    await notify_admins(
        m.bot,
        f"{E('deposit')} <b>New deposit #{did}</b>\nUser: <code>{m.from_user.id}</code> (@{esc(m.from_user.username or '-')})\n"
        f"Method: {esc(d['method'])}\nAmount: <b>{money(d['amount'])}</b>\nTrx: <code>{esc(trx)}</code>",
        kb([B("Approve", f"adm:dok:{did}", "success", "ok"), B("Reject", f"adm:dno:{did}", "danger", "no")]))


# ════════════════════════════════════════════════════════════════════
#  ADMIN ROUTER
# ════════════════════════════════════════════════════════════════════
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class AddP(StatesGroup):
    name = State()
    desc = State()
    price = State()


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


def admin_kb():
    return kb(
        [B("Statistics", "adm:stats", "primary", "stats"), B("Deposits", "adm:deps", "success", "deposit")],
        [B("Products", "adm:prods", "primary", "stock"), B("Add Product", "adm:addp", "success", "add")],
        [B("Users", "adm:users", None, "users"), B("Broadcast", "adm:bc", "primary", "broadcast")],
        [B("Set Notice", "adm:notice", None, "notice"), B("Settings", "adm:set", None, "settings")],
        [B("Exit Admin", "home", "danger", "home")],
    )


ADMIN_BACK = kb([B("Admin Panel", "adm:home", None, "admin")])


@admin.message(Command("admin"))
async def adm_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(f"{E('admin')} <b>Admin Panel</b>", reply_markup=admin_kb())


@admin.callback_query(F.data == "adm:home")
async def adm_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, f"{E('admin')} <b>Admin Panel</b>\n<i>Manage your store</i>", admin_kb())


# ── Stats ───────────────────────────────────────────────────────────
@admin.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    today = int(time.time()) - 86400
    g = lambda r: r[0] or 0  # noqa
    users = g(await q_one("SELECT COUNT(*) FROM users"))
    new24 = g(await q_one("SELECT COUNT(*) FROM users WHERE joined_at>?", today))
    prods = g(await q_one("SELECT COUNT(*) FROM products"))
    stk = g(await q_one("SELECT COUNT(*) FROM stock WHERE sold=0"))
    orders = g(await q_one("SELECT COUNT(*) FROM orders"))
    rev = g(await q_one("SELECT SUM(price) FROM orders"))
    rev24 = g(await q_one("SELECT SUM(price) FROM orders WHERE created_at>?", today))
    pend = g(await q_one("SELECT COUNT(*) FROM deposits WHERE status='pending'"))
    dep = g(await q_one("SELECT SUM(amount) FROM deposits WHERE status='approved'"))
    bal = g(await q_one("SELECT SUM(balance) FROM users"))
    t = (f"{E('stats')} <b>Statistics</b>\n\n"
         f"{E('users')} Users: <b>{users}</b> (+{new24} today)\n"
         f"{E('stock')} Products: <b>{prods}</b> · Stock left: <b>{stk}</b>\n"
         f"{E('orders')} Orders: <b>{orders}</b>\n"
         f"{E('cart')} Revenue: <b>{money(rev)}</b> (24h: {money(rev24)})\n"
         f"{E('deposit')} Deposits approved: <b>{money(dep)}</b>\n"
         f"⏳ Pending deposits: <b>{pend}</b>\n"
         f"{E('wallet')} Users' total balance: <b>{money(bal)}</b>")
    await show(c, t, kb([B("Refresh", "adm:stats", "primary", "bolt")], [B("Admin Panel", "adm:home", None, "admin")]))


# ── Products ────────────────────────────────────────────────────────
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
    cur = await q_run("INSERT INTO products(category,name,description,price) VALUES(?,?,?,?)",
                      d["cat"], d["name"], d["desc"], price)
    await state.clear()
    await m.answer(f"{E('ok')} Product created!",
                   reply_markup=kb([B("Add Stock", f"adm:stk:{cur.lastrowid}", "success", "stock")],
                                   [B("Admin Panel", "adm:home", None, "admin")]))


@admin.callback_query(F.data == "adm:prods")
async def adm_prods(c: CallbackQuery):
    rows = await q_all(
        """SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id AND s.sold=0) AS stk
           FROM products p ORDER BY p.id DESC LIMIT 40""")
    btns = [[B(f"{'🟢' if p['active'] else '🔴'} #{p['id']} {p['name']} · {money(p['price'])} · {p['stk']}",
               f"adm:p:{p['id']}")] for p in rows]
    btns.append([B("Add Product", "adm:addp", "success", "add")])
    btns.append([B("Admin Panel", "adm:home", None, "admin")])
    await show(c, f"{E('stock')} <b>Products</b> <i>(🟢 active · 🔴 hidden · stock)</i>", kb(*btns))


async def render_admin_product(c: CallbackQuery, pid: int):
    p = await product_row(pid)
    if not p:
        return await c.answer("Not found", show_alert=True)
    sold = (await q_one("SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=1", p["id"]))[0]
    t = (f"{E(p['category'])} <b>#{p['id']} {esc(p['name'])}</b>\n\n{esc(p['description'] or '')}\n\n"
         f"Price: <b>{money(p['price'])}</b>\nIn stock: <b>{p['stk']}</b> · Sold: <b>{sold}</b>\n"
         f"Status: {'🟢 Active' if p['active'] else '🔴 Hidden'}")
    await show(c, t, kb(
        [B("Add Stock", f"adm:stk:{p['id']}", "success", "stock"), B("Edit Price", f"adm:ep:{p['id']}", "primary", "edit")],
        [B("Hide" if p["active"] else "Show", f"adm:tg:{p['id']}", None, "lock"),
         B("Delete", f"adm:del:{p['id']}", "danger", "trash")],
        [B("Back", "adm:prods", None, "back")]))


@admin.callback_query(F.data.startswith("adm:p:"))
async def adm_product(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_admin_product(c, int(c.data.split(":")[2]))


@admin.callback_query(F.data.startswith("adm:tg:"))
async def adm_toggle(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    await q_run("UPDATE products SET active=1-active WHERE id=?", pid)
    await render_admin_product(c, pid)


@admin.callback_query(F.data.startswith("adm:del:"))
async def adm_del(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    await show(c, "⚠️ <b>Delete this product and all its unsold stock?</b>",
               kb([B("Yes, delete", f"adm:delok:{pid}", "danger", "trash"), B("No", f"adm:p:{pid}", "success", "no")]))


@admin.callback_query(F.data.startswith("adm:delok:"))
async def adm_delok(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    await q_run("DELETE FROM stock WHERE product_id=? AND sold=0", pid)
    await q_run("DELETE FROM products WHERE id=?", pid)
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
    await q_run("UPDATE products SET price=? WHERE id=?", price, d["pid"])
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
    d = await state.get_data()
    text = (m.text or "").strip()
    items = [x.strip() for x in re.split(r"\n-{3,}\n", text)] if re.search(r"\n-{3,}\n", text) \
        else [x.strip() for x in text.splitlines()]
    items = [x for x in items if x]
    if not items:
        return await m.answer("❗ Nothing to add.")
    await db.executemany("INSERT INTO stock(product_id,data) VALUES(?,?)", [(d["pid"], x) for x in items])
    await db.commit()
    await state.clear()
    await m.answer(f"{E('ok')} Added <b>{len(items)}</b> items to stock.",
                   reply_markup=kb([B("Back to product", f"adm:p:{d['pid']}", None, "back")],
                                   [B("Admin Panel", "adm:home", None, "admin")]))


# ── Deposits ────────────────────────────────────────────────────────
@admin.callback_query(F.data == "adm:deps")
async def adm_deps(c: CallbackQuery):
    rows = await q_all("SELECT * FROM deposits WHERE status='pending' ORDER BY id LIMIT 20")
    if not rows:
        return await show(c, f"{E('deposit')} <b>Pending Deposits</b>\n\n<i>Nothing pending 🎉</i>", ADMIN_BACK)
    btns = [[B(f"#{d['id']} · {money(d['amount'])} · {d['method']} · {d['user_id']}", f"adm:d:{d['id']}", "primary")]
            for d in rows]
    btns.append([B("Admin Panel", "adm:home", None, "admin")])
    await show(c, f"{E('deposit')} <b>Pending Deposits</b>", kb(*btns))


@admin.callback_query(F.data.startswith("adm:d:"))
async def adm_dep(c: CallbackQuery):
    d = await q_one("SELECT * FROM deposits WHERE id=?", int(c.data.split(":")[2]))
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
    did = int(c.data.split(":")[2])
    async with buy_lock:
        d = await q_one("SELECT * FROM deposits WHERE id=?", did)
        if not d or d["status"] != "pending":
            return await c.answer("Already handled.", show_alert=True)
        await db.execute("UPDATE deposits SET status='approved' WHERE id=?", (did,))
        await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (d["amount"], d["user_id"]))
        await db.commit()
    u = await get_user(d["user_id"])
    with_note = f"\n\n{E('ok')} <b>Approved by {esc(c.from_user.full_name)}</b>"
    try:
        await c.message.edit_text(c.message.html_text + with_note)
    except Exception:
        pass
    await c.answer("Approved ✅")
    try:
        await c.bot.send_message(
            d["user_id"],
            f"{E('ok')} <b>Deposit approved!</b>\n\n+{money(d['amount'])} added to your wallet.\n"
            f"New balance: <b>{money(u['balance'])}</b>",
            reply_markup=kb([B("Buy Proxy", "cat:proxy", "primary", "proxy"), B("Buy VPN", "cat:vpn", "primary", "vpn")]))
    except Exception:
        pass


@admin.callback_query(F.data.startswith("adm:dno:"))
async def adm_dno(c: CallbackQuery):
    did = int(c.data.split(":")[2])
    d = await q_one("SELECT * FROM deposits WHERE id=?", did)
    if not d or d["status"] != "pending":
        return await c.answer("Already handled.", show_alert=True)
    await q_run("UPDATE deposits SET status='rejected' WHERE id=?", did)
    try:
        await c.message.edit_text(c.message.html_text + f"\n\n{E('no')} <b>Rejected by {esc(c.from_user.full_name)}</b>")
    except Exception:
        pass
    await c.answer("Rejected ❌")
    try:
        await c.bot.send_message(d["user_id"],
                                 f"{E('no')} <b>Deposit #{did} rejected</b>\nIf you think this is a mistake, please contact support.")
    except Exception:
        pass


# ── Users ───────────────────────────────────────────────────────────
@admin.callback_query(F.data == "adm:users")
async def adm_users(c: CallbackQuery, state: FSMContext):
    await state.set_state(FindUser.query)
    await show(c, f"{E('users')} <b>Users</b>\n\nSend a <b>user ID</b> or <b>@username</b> to manage:",
               kb([B("Cancel", "adm:home", "danger", "no")]))


async def user_panel(uid: int):
    u = await get_user(uid)
    if not u:
        return None, None
    n = (await q_one("SELECT COUNT(*) FROM orders WHERE user_id=?", uid))[0]
    t = (f"{E('profile')} <b>{esc(u['full_name'] or '-')}</b> (@{esc(u['username'] or '-')})\n"
         f"ID: <code>{uid}</code>\nBalance: <b>{money(u['balance'])}</b>\nSpent: {money(u['spent'])}\n"
         f"Orders: {n}\nJoined: {fdate(u['joined_at'])}\nStatus: {'🚫 Banned' if u['banned'] else '✅ Active'}")
    markup = kb(
        [B("Add Balance", f"adm:adj:{uid}:+", "success", "add"), B("Deduct", f"adm:adj:{uid}:-", "danger", "no")],
        [B("Unban" if u["banned"] else "Ban", f"adm:ban:{uid}", "danger" if not u["banned"] else "success", "ban")],
        [B("Admin Panel", "adm:home", None, "admin")])
    return t, markup


@admin.message(StateFilter(FindUser.query))
async def find_user(m: Message, state: FSMContext):
    q = (m.text or "").strip().lstrip("@")
    row = await q_one("SELECT id FROM users WHERE id=?", int(q)) if q.isdigit() \
        else await q_one("SELECT id FROM users WHERE lower(username)=lower(?)", q)
    if not row:
        return await m.answer("❗ User not found. Try again or /cancel")
    await state.clear()
    t, mk = await user_panel(row["id"])
    await m.answer(t, reply_markup=mk)


@admin.callback_query(F.data.startswith("adm:ban:"))
async def adm_ban(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    await q_run("UPDATE users SET banned=1-banned WHERE id=?", uid)
    t, mk = await user_panel(uid)
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
    await q_run("UPDATE users SET balance=balance+? WHERE id=?", delta, d["uid"])
    await state.clear()
    t, mk = await user_panel(d["uid"])
    await m.answer(f"{E('ok')} Balance updated.\n\n{t}", reply_markup=mk)
    try:
        sign = "+" if delta >= 0 else "-"
        await m.bot.send_message(d["uid"], f"{E('wallet')} Your balance was adjusted by <b>{sign}{money(abs(delta))}</b> by admin.")
    except Exception:
        pass


# ── Broadcast & Notice ──────────────────────────────────────────────
@admin.callback_query(F.data == "adm:bc")
async def adm_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.msg)
    await show(c, f"{E('broadcast')} <b>Broadcast</b>\n\nSend the message you want to deliver to <b>all users</b>.\n"
                  "<i>Text, photo, video, custom emoji, buttons-free formatting — everything is copied as is.</i>",
               kb([B("Cancel", "adm:home", "danger", "no")]))


@admin.message(StateFilter(Broadcast.msg))
async def bc_preview(m: Message, state: FSMContext):
    n = (await q_one("SELECT COUNT(*) FROM users WHERE banned=0"))[0]
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
    users = await q_all("SELECT id FROM users WHERE banned=0")
    await show(c, f"📤 Sending to {len(users)} users…")
    ok = fail = 0
    for u in users:
        for attempt in range(2):
            try:
                await c.bot.copy_message(u["id"], d["chat"], d["mid"])
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
    cur = SET.get("notice") or "<i>(none)</i>"
    await show(c, f"{E('notice')} <b>Notice Board</b>\n\nCurrent:\n{cur}\n\n"
                  "Send the new notice (formatting &amp; custom emoji supported).\nSend <code>clear</code> to remove it.",
               kb([B("Cancel", "adm:home", "danger", "no")]))


@admin.message(StateFilter(NoticeS.text))
async def notice_save(m: Message, state: FSMContext):
    await state.clear()
    if (m.text or "").strip().lower() == "clear":
        await del_setting("notice")
        return await m.answer(f"{E('ok')} Notice cleared.", reply_markup=ADMIN_BACK)
    await set_setting("notice", m.html_text)
    await m.answer(f"{E('ok')} Notice updated! It now shows on the home screen.",
                   reply_markup=kb([B("Also broadcast it", "adm:bc", "primary", "broadcast")],
                                   [B("Admin Panel", "adm:home", None, "admin")]))


# ── Settings ────────────────────────────────────────────────────────
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
    t = (f"{E('settings')} <b>Settings</b>\n\n"
         f"Support: <b>{esc(SET.get('support') or '-')}</b>\n"
         f"Min deposit: <b>{money(min_deposit())}</b>\n"
         f"Payment methods: <b>{len(pay_methods())}</b>\n"
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


# ── Custom emoji IDs for buttons ────────────────────────────────────
@admin.callback_query(F.data == "adm:emo")
async def adm_emo(c: CallbackQuery, state: FSMContext):
    await state.clear()
    keys = list(EMOJI.keys())
    rows, row = [], []
    for k in keys:
        mark = "✅" if emoji_id(k) else "·"
        row.append(B(f"{EMOJI[k]} {k} {mark}", f"adm:emk:{k}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("Settings", "adm:set", None, "settings")])
    t = (f"{E('star')} <b>Custom Emoji IDs</b>\n\n"
         "Pick a slot, then <b>send one custom emoji</b> (from any Premium pack) — the bot reads its ID automatically. "
         "You can also send a numeric ID, or <code>clear</code>.\n\n"
         "✅ = custom emoji set\n"
         "<i>Buttons use it as icon · messages use it inline.</i>")
    await show(c, t, kb(*rows))


@admin.callback_query(F.data.startswith("adm:emk:"))
async def adm_emk(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.set_state(EmojiS.value)
    await state.update_data(key=key)
    cur = emoji_id(key) or "none"
    await show(c, f"{EMOJI[key]} <b>{key}</b>\nCurrent ID: <code>{cur}</code>\n\n"
                  "Send a <b>custom emoji</b> now (or its numeric ID / <code>clear</code>).",
               kb([B("Cancel", "adm:emo", "danger", "no")]))


@admin.message(StateFilter(EmojiS.value))
async def emo_save(m: Message, state: FSMContext):
    d = await state.get_data()
    key = d["key"]
    cid = None
    for ent in (m.entities or []):
        if ent.type == "custom_emoji":
            cid = ent.custom_emoji_id
            break
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
    await m.answer(f"{E('ok')} <b>{key}</b> → <code>{cid}</code>\nShown below with the new emoji: {E(key)}",
                   reply_markup=kb([B("Test button", "adm:emo", "success", key),
                                    B("Emoji list", "adm:emo", None, "star")]))


# ════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════
async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN is missing. Copy .env.example to .env and fill it in.")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty — nobody can open the admin panel!")
    await init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Guard())
    dp.callback_query.outer_middleware(Guard())
    dp.include_router(admin)     # admin first so FSM states are matched correctly
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Open main menu"),
        BotCommand(command="cancel", description="Cancel current action"),
    ])
    me = await bot.get_me()
    log.info("Bot started as @%s", me.username)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
