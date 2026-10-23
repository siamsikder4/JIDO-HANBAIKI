# -*- coding: utf-8 -*-
"""
Your Proxy Shop Bot  (v3, Firebase storage)
-------------------------------------------
NEW in v3
* Auto deposit ONLY via Binance Pay API (Order ID is verified automatically, balance added instantly)
* bKash / Nagad (any other method) -> admin verifies manually, user may attach an optional screenshot
* Duplicate Transaction-ID check (+ duplicate screenshot check)
* Low-stock alert (admins/sub-admins get a message when a product runs low)
* Transaction history (user + admin per-user view)
* Sales report (24h / 7d / 30d / all) + CSV export
* Sub-admin roles with per-permission toggles
* Targeted broadcast (buyers, non-buyers, has balance, active 7d, new 7d, custom IDs)
* Stock import from .txt / .csv file (duplicates are skipped automatically)
* Text anti-spam (flood + repeated text -> temporary mute)
* Every new button / message emoji can be changed from Admin Panel -> Custom Emoji

Env vars: BOT_TOKEN, ADMIN_IDS (owners), BRAND_NAME, CURRENCY, FORCE_CHANNEL,
          BINANCE_API_KEY, BINANCE_API_SECRET  (read-only key with "Enable Reading" is enough)
Requires: aiogram >= 3.25, firebase-admin, python-dotenv, aiohttp
"""
import asyncio
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import time
from collections import deque
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
    BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

load_dotenv()

# ═════════════ CONFIG ═════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}   # OWNERS (full access)
BRAND = os.getenv("BRAND_NAME", "Your Proxy")
CURRENCY = os.getenv("CURRENCY", "BDT")
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "").strip()
# ── Firebase (Firestore) service-account credentials ──
# Option A: paste your service-account JSON between the braces below (keep this file PRIVATE, never upload to GitHub)
FIREBASE_JSON = {
}
# Option B: env var FIREBASE_CRED_JSON (whole JSON) or a file path in FIREBASE_CRED
FIREBASE_CRED = os.getenv("FIREBASE_CRED", "firebase.json")
FIREBASE_CRED_JSON = os.getenv("FIREBASE_CRED_JSON", "").strip()
STORE_FILE = os.getenv("STORE_FILE", "data.json")                  # fallback only

# ── Binance Pay auto-deposit ──
BINANCE_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
BINANCE_HOST = os.getenv("BINANCE_HOST", "https://api.binance.com").rstrip("/")
BINANCE_ON = bool(BINANCE_KEY and BINANCE_SECRET)
STABLE = {"USDT", "USDC", "BUSD", "FDUSD"}          # currencies treated as 1 USD

# ── Anti-spam ──
SPAM_WINDOW = 6        # seconds
SPAM_MAX = 8           # max events (messages + button taps) per window
SPAM_REPEAT = 4        # same text this many times within 10s = spam
SPAM_MUTE = 30         # seconds (multiplied by strikes, max 600)

# Every button: key -> (label, default color, fallback emoji, group)
# color: "primary" (blue) | "success" (green) | "danger" (red) | None (default)
BTN = {
    # user - bottom menu
    "buy": ("Buy Product", "success", "🛒", "user"),
    "my_profile": ("My Profile", "primary", "👤", "user"),
    "deposit": ("Deposit", "primary", "💰", "user"),
    "history": ("Transactions", "primary", "📜", "user"),
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
    "skip": ("Skip screenshot", "primary", "⏭", "user"),
    # admin - bottom menu
    "a_dash": ("Dashboard", "primary", "📊", "admin"),
    "a_report": ("Sales Report", "primary", "📈", "admin"),
    "a_users": ("Users", "primary", "👥", "admin"),
    "a_prod": ("Products", "success", "🗃", "admin"),
    "a_cat": ("Categories", "primary", "📁", "admin"),
    "a_deps": ("Deposits", "success", "💰", "admin"),
    "a_bc": ("Broadcast", "primary", "📣", "admin"),
    "a_notice": ("Notice", None, "📢", "admin"),
    "a_set": ("Settings", None, "⚙️", "admin"),
    "a_subs": ("Sub Admins", "primary", "🛡", "admin"),
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
    "x_refresh": ("Refresh", "primary", "🔄", "admin"),
    "x_low": ("Low Stock", "danger", "⚠️", "admin"),
    "x_csv": ("Export CSV", "primary", "📤", "admin"),
    "x_import": ("Add / Import Stock", "success", "📥", "admin"),
    "x_hist": ("Transactions / History", "primary", "📜", "admin"),
    "x_shot": ("View Screenshot", "primary", "🖼", "admin"),
    "x_aud": ("Broadcast audience", "primary", "🎯", "admin"),
    "x_period": ("Report period buttons", None, "📅", "admin"),
    "x_sub": ("Sub-admin list items", None, "🛡", "admin"),
}
USER_KEYS = ["buy", "my_profile", "deposit", "history", "code", "support", "admin"]
ADMIN_KEYS = ["a_dash", "a_report", "a_users", "a_prod", "a_cat", "a_deps", "a_bc", "a_notice", "a_set",
              "a_subs", "a_style", "a_emoji", "a_exit"]

# Sub-admin permissions
PERMS = {
    "dash": "Dashboard & Reports", "users": "Users", "prod": "Products & Stock", "cat": "Categories",
    "deps": "Deposits", "bc": "Broadcast", "notice": "Notice", "set": "Settings",
}
# bottom-menu key -> permission ("owner" = owners only, "" = everyone staff)
KEY_PERM = {
    "a_dash": "dash", "a_report": "dash", "a_users": "users", "a_prod": "prod", "a_cat": "cat",
    "a_deps": "deps", "a_bc": "bc", "a_notice": "notice", "a_set": "set",
    "a_subs": "owner", "a_style": "owner", "a_emoji": "owner", "a_exit": "",
}
# inline admin callback op ("adm:<op>:...") -> permission
OP_PERM = {
    "stats": "dash", "rep": "dash", "repcsv": "dash", "low": "dash",
    "cats": "cat", "addc": "cat", "c": "cat", "crn": "cat", "cdel": "cat", "cem": "cat",
    "pl": "prod", "addp": "prod", "addpc": "prod", "p": "prod", "tg": "prod", "del": "prod", "delok": "prod",
    "ep": "prod", "ed": "prod", "stk": "prod", "pem": "prod",
    "deps": "deps", "d": "deps", "dok": "deps", "dno": "deps", "dh": "deps", "dss": "deps",
    "ul": "users", "find": "users", "u": "users", "ban": "users", "uo": "users", "o": "users",
    "adj": "users", "ut": "users",
    "bca": "bc", "bcgo": "bc",
    "set": "set", "tgl": "set", "sv": "set",
    "sty": "owner", "stp": "owner", "sts": "owner", "emo": "owner", "emk": "owner",
    "subs": "owner", "sa": "owner", "saadd": "owner", "sap": "owner", "sadel": "owner", "sadelok": "owner",
}

# Emojis used inside message text (custom emoji can be set for each from admin panel)
EMOJI = {
    "fire": "🔥", "ok": "✅", "no": "❌", "wallet": "💳", "profile": "👤", "lock": "🔒",
    "notice": "📢", "stock": "🗃", "orders": "📦", "cart": "🛒", "bolt": "⚡", "party": "🎉",
    "stats": "📊", "users": "👥", "ban": "🚫", "settings": "⚙️", "invoice": "🧾",
    "money": "💵", "date": "📅", "crown": "👑", "broadcast": "📣", "star": "⭐", "down": "⬇️",
    # new in v3
    "report": "📈", "low": "⚠️", "target": "🎯", "shield": "🛡", "hist": "📜", "photo": "🖼",
    "pending": "⏳", "auto": "🤖", "spam": "🚨", "credit": "🟢", "debit": "🔴", "import": "📥",
    "id": "🆔", "rank": "🏅", "name": "📛", "link": "🔗", "binance": "🟡",
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
D: dict[str, dict] = {k: {} for k in ("users", "cats", "products", "stock", "orders", "deposits",
                                      "txns", "subs", "settings", "seq")}
SET = D["settings"]
store_lock = asyncio.Lock()
buy_lock = asyncio.Lock()
dep_lock = asyncio.Lock()
fb_ref = None
ADMIN_MODE: set[int] = set()     # staff currently showing the admin bottom menu
LOW_SENT: dict[int, int] = {}    # product id -> stock level when last alert was sent


def _norm(v) -> dict:
    if isinstance(v, list):
        return {str(i): x for i, x in enumerate(v) if x is not None}
    if isinstance(v, dict):
        return {str(k): x for k, x in v.items()}
    return {}


def _fb_creds():
    if FIREBASE_JSON.get("private_key"):
        return FIREBASE_JSON
    if FIREBASE_CRED_JSON:
        return json.loads(FIREBASE_CRED_JSON, strict=False)
    for path in (FIREBASE_CRED, "/etc/secrets/firebase.json", "/etc/secrets/" + os.path.basename(FIREBASE_CRED)):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    return None


def _col(coll: str):
    return fb_ref.collection(f"shop_{coll}")


def init_store():
    global fb_ref
    data = {}
    creds = _fb_creds()
    if creds:
        import firebase_admin
        from firebase_admin import credentials, firestore
        firebase_admin.initialize_app(credentials.Certificate(creds))
        fb_ref = firestore.client()
        for coll in D:
            part = {}
            for doc in _col(coll).stream():
                v = doc.to_dict() or {}
                part[doc.id] = v["value"] if coll in ("settings", "seq") else v
            data[coll] = part
        log.info("Firebase Firestore connected")
    else:
        log.warning("Firebase not configured -> using local file %s (data is lost on redeploy on Render!)", STORE_FILE)
        if os.path.exists(STORE_FILE):
            with open(STORE_FILE, encoding="utf-8") as f:
                data = json.load(f)
    for coll in D:
        D[coll].clear()
        D[coll].update(_norm(data.get(coll)))


def _persist(coll: str, keys: tuple):
    if fb_ref:
        col = _col(coll)
        for i in range(0, len(keys), 400):
            batch = fb_ref.batch()
            for k in keys[i:i + 400]:
                val = D[coll].get(k)
                if val is None:
                    batch.delete(col.document(k))
                else:
                    batch.set(col.document(k), {"value": val} if coll in ("settings", "seq") else val)
            batch.commit()
    else:
        tmp = STORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(D, f, ensure_ascii=False)
        os.replace(tmp, STORE_FILE)


async def put(coll: str, *keys):
    if not keys:
        return
    async with store_lock:
        await asyncio.to_thread(_persist, coll, tuple(str(k) for k in keys))


async def nid(name: str) -> int:
    D["seq"][name] = int(D["seq"].get(name, 0)) + 1
    await put("seq", name)
    return D["seq"][name]


async def nids(name: str, n: int) -> list[int]:
    """Reserve n ids with ONE write (fast bulk import)."""
    start = int(D["seq"].get(name, 0))
    D["seq"][name] = start + n
    await put("seq", name)
    return list(range(start + 1, start + n + 1))


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


# ── roles ──
def is_owner(uid) -> bool:
    return uid in ADMIN_IDS


def is_staff(uid) -> bool:
    return uid in ADMIN_IDS or str(uid) in D["subs"]


def can(uid, perm) -> bool:
    if uid in ADMIN_IDS or not perm:
        return uid in ADMIN_IDS or is_staff(uid) if not perm else True
    if perm == "owner":
        return False
    return perm in D["subs"].get(str(uid), {}).get("perms", [])


def staff_ids(perm=None) -> set:
    """Owners + sub-admins that hold `perm` (owners only if perm is None)."""
    ids = set(ADMIN_IDS)
    if perm:
        for k, s in D["subs"].items():
            if perm in s.get("perms", []):
                ids.add(int(k))
    return ids


async def touch_user(u):
    row = get_user(u.id)
    name, un = u.full_name or "", u.username or ""
    now = int(time.time())
    if row is None:
        row = {"id": u.id, "username": un, "full_name": name, "balance": 0.0,
               "spent": 0.0, "banned": 0, "joined_at": now, "seen": now}
        D["users"][str(u.id)] = row
        await put("users", u.id)
    else:
        dirty = False
        if row.get("username") != un or row.get("full_name") != name:
            row["username"], row["full_name"] = un, name
            dirty = True
        if now - int(row.get("seen", 0)) > 3600:
            row["seen"] = now
            dirty = True
        if dirty:
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
    rows = [[RB("buy")], [RB("my_profile"), RB("deposit")], [RB("history"), RB("code")], [RB("support")]]
    if is_staff(uid):
        rows.append([RB("admin")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Choose an option 👇")


def admin_menu_kb(uid: int) -> ReplyKeyboardMarkup:
    keys = [k for k in ADMIN_KEYS if k != "a_exit" and can(uid, KEY_PERM[k])]
    rows = [[RB(k) for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
    rows.append([RB("a_exit")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Admin Panel 👑")


def menu_kb(uid: int) -> ReplyKeyboardMarkup:
    return admin_menu_kb(uid) if (is_staff(uid) and uid in ADMIN_MODE) else user_menu_kb(uid)


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


async def notify_admins(bot: Bot, text: str, markup=None, perm=None, photo=None):
    """Send to owners + sub-admins holding `perm` (owners only when perm is None)."""
    for aid in staff_ids(perm):
        try:
            if photo:
                await bot.send_photo(aid, photo, caption=text, reply_markup=markup)
            else:
                await bot.send_message(aid, text, reply_markup=markup)
        except Exception as e:
            log.warning("Cannot notify admin %s: %s", aid, e)


async def stamp(c: CallbackQuery, extra: str):
    """Append a status line to the notification (works for text AND photo messages)."""
    base = c.message.html_text or ""
    try:
        if c.message.photo:
            await c.message.edit_caption(caption=base + extra, reply_markup=None)
        else:
            await c.message.edit_text(base + extra, reply_markup=None)
    except Exception:
        pass


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


def usd_rate() -> float:
    try:
        return float(SET.get("usd_rate", "120"))
    except ValueError:
        return 120.0


def low_threshold() -> int:
    try:
        return int(float(SET.get("low_stock", "5")))
    except ValueError:
        return 5


TOGGLES = {"maintenance": "0", "antispam": "1", "auto_dep": "1"}   # setting -> default


def flag(key: str) -> bool:
    return SET.get(key, TOGGLES.get(key, "0")) == "1"


def is_auto(method_name: str) -> bool:
    """Only Binance Pay is auto-verified (needs API keys + toggle ON)."""
    return BINANCE_ON and flag("auto_dep") and "binance" in method_name.lower()


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


def desc_html(p) -> str:
    """Descriptions saved by the admin keep formatting + custom emoji (stored as HTML)."""
    d = p.get("description") or ""
    return d if p.get("dhtml") else esc(d)


def rank_of(spent: float) -> str:
    if spent >= 5000:
        return "💎 Diamond"
    if spent >= 1000:
        return "🥇 Gold"
    if spent >= 200:
        return "🥈 Silver"
    return "🥉 Bronze"


# ── duplicate checks ──
def trx_key(t: str) -> str:
    return re.sub(r"\s+", "", t or "").upper()


def trx_used(trx: str):
    """A transaction id is 'used' if any pending/approved deposit already has it."""
    k = trx_key(trx)
    for d in D["deposits"].values():
        if d["status"] != "rejected" and trx_key(d.get("trx", "")) == k:
            return d
    return None


def ss_used(uniq: str) -> bool:
    return any(d.get("ss_uid") == uniq and d["status"] != "rejected" for d in D["deposits"].values())


# ── ledger ──
TX_LABEL = {"deposit": "Deposit", "purchase": "Purchase", "admin_add": "Admin credit", "admin_deduct": "Admin debit"}


async def log_txn(uid, typ: str, amount: float, note: str = "", bal=None):
    tid = await nid("txns")
    u = get_user(uid) or {}
    D["txns"][str(tid)] = {"id": tid, "user_id": uid, "type": typ, "amount": float(amount),
                           "balance_after": float(u.get("balance", 0) if bal is None else bal),
                           "note": note, "created_at": int(time.time())}
    await put("txns", tid)


def history_lines(uid):
    out = []
    for x in D["txns"].values():
        if x["user_id"] != uid:
            continue
        amt = float(x["amount"])
        icon = E("credit") if amt >= 0 else E("debit")
        sign = "+" if amt >= 0 else "-"
        note = f" · {esc(x['note'])}" if x.get("note") else ""
        out.append((x["created_at"],
                    f"{icon} <b>{sign}{money(abs(amt))}</b> · {TX_LABEL.get(x['type'], x['type'])}{note}\n"
                    f"    <i>{fdate(x['created_at'])} · Balance {money(x['balance_after'])}</i>"))
    for d in D["deposits"].values():
        if d["user_id"] == uid and d["status"] in ("pending", "rejected"):
            icon = E("pending") if d["status"] == "pending" else E("no")
            out.append((d["created_at"],
                        f"{icon} <b>{money(d['amount'])}</b> · Deposit #{d['id']} ({d['status'].title()})\n"
                        f"    <i>{fdate(d['created_at'])} · {esc(d['method'])}</i>"))
    out.sort(key=lambda x: -x[0])
    return out


async def screen_history(ev, uid, page=0, prefix="txh", back=None):
    items, page, pages = paginate(history_lines(uid), page, 8)
    body = "\n\n".join(l for _, l in items) if items else "<i>No transactions yet.</i>"
    t = f"{E('hist')} <b>Transaction History</b>\n{BAR}\n{body}"
    rows = []
    nr = nav_row(prefix, page, pages)
    if nr:
        rows.append(nr)
    if back:
        rows.append([back])
    await show(ev, t, kb(*rows) if rows else None)


# ── low stock ──
def low_list():
    th = low_threshold()
    rows = [product_row(p["id"]) for p in D["products"].values() if p.get("active")]
    return sorted((p for p in rows if p["stk"] <= th), key=lambda p: p["stk"])


async def check_low(bot: Bot, pid: int):
    """Alert staff once when stock drops to the limit, again when it hits zero."""
    p = product_row(pid)
    if not p:
        return
    if p["stk"] > low_threshold():
        LOW_SENT.pop(pid, None)
        return
    last = LOW_SENT.get(pid)
    if last is None or (p["stk"] == 0 and last > 0):
        LOW_SENT[pid] = p["stk"]
        head = "OUT OF STOCK" if p["stk"] == 0 else "Low stock"
        await notify_admins(bot, f"{E('low')} <b>{head}</b>\n{esc(p['name'])} — <b>{p['stk']}</b> left "
                                 f"(limit {low_threshold()})", perm="prod")


# ── Binance Pay verification ──
async def binance_find(order_id: str):
    """Look the Order ID up in Binance Pay history. Returns ('ok', tx) | ('notfound', None) | ('error', None)."""
    import aiohttp
    oid = order_id.strip().upper()
    bounds = [0, 1, 3, 7, 15, 30]               # days back, searched newest first
    now = int(time.time() * 1000)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            for a, b in zip(bounds, bounds[1:]):
                end, start = now - a * 86400000, now - b * 86400000
                qs = (f"startTimestamp={start}&endTimestamp={end}&limit=100"
                      f"&recvWindow=30000&timestamp={int(time.time() * 1000)}")
                sig = hmac.new(BINANCE_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
                async with s.get(f"{BINANCE_HOST}/sapi/v1/pay/transactions?{qs}&signature={sig}",
                                 headers={"X-MBX-APIKEY": BINANCE_KEY}) as r:
                    js = await r.json(content_type=None)
                ok = isinstance(js, dict) and (js.get("success") is True or str(js.get("code")) == "000000")
                if not ok:
                    log.warning("Binance API error: %s", js)
                    return "error", None
                for tx in js.get("data") or []:
                    ids = {str(tx.get("transactionId", "")).upper(), str(tx.get("orderId", "")).upper()}
                    if oid in ids:
                        return "ok", tx
    except Exception as e:
        log.warning("Binance request failed: %s", e)
        return "error", None
    return "notfound", None


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


class AntiSpam(BaseMiddleware):
    """Flood + repeated-text protection. Staff are exempt."""

    def __init__(self):
        self.hits: dict[int, deque] = {}
        self.mute: dict[int, float] = {}
        self.strikes: dict[int, tuple] = {}      # uid -> (count, last_time)
        self.last: dict[int, tuple] = {}         # uid -> (text, count, first_time)

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if not u or is_staff(u.id) or not flag("antispam"):
            return await handler(event, data)
        now = time.monotonic()
        until = self.mute.get(u.id, 0)
        if now < until:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer(f"⏳ Slow down! Wait {int(until - now) + 1}s")
                except Exception:
                    pass
            return
        dq = self.hits.setdefault(u.id, deque())
        dq.append(now)
        while dq and now - dq[0] > SPAM_WINDOW:
            dq.popleft()
        bad = len(dq) > SPAM_MAX
        if isinstance(event, Message) and event.text:
            key = event.text.strip().lower()
            last = self.last.get(u.id)
            if last and last[0] == key and now - last[2] < 10:
                cnt, first = last[1] + 1, last[2]
            else:
                cnt, first = 1, now
            self.last[u.id] = (key, cnt, first)
            if cnt >= SPAM_REPEAT:
                bad = True
        if bad:
            cnt, ts = self.strikes.get(u.id, (0, 0))
            cnt = 1 if now - ts > 600 else cnt + 1
            self.strikes[u.id] = (cnt, now)
            secs = min(SPAM_MUTE * cnt, 600)
            self.mute[u.id] = now + secs
            dq.clear()
            self.last.pop(u.id, None)
            log.info("Spam mute: user %s for %ss (strike %s)", u.id, secs, cnt)
            txt = f"🚨 <b>Spam detected.</b> Please wait <b>{secs}s</b> before using the bot again."
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(f"🚨 Spam detected. Wait {secs}s", show_alert=True)
                else:
                    await event.answer(txt)
            except Exception:
                pass
            return
        if len(self.hits) > 5000:                # keep memory small
            for k in [k for k, v in self.hits.items() if not v]:
                self.hits.pop(k, None)
        return await handler(event, data)


class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if not u:
            return await handler(event, data)
        row = await touch_user(u)
        if is_staff(u.id):
            await refresh_menu(event, u, row)
            return await handler(event, data)
        msg = event.message if isinstance(event, CallbackQuery) else event

        async def deny(text, markup=None):
            if isinstance(event, CallbackQuery):
                await event.answer()
            await msg.answer(text, reply_markup=markup)

        if row["banned"]:
            return await deny(f"{E('ban')} <b>You are banned from using this bot.</b>")
        if flag("maintenance"):
            return await deny(f"{E('settings')} <b>Maintenance mode</b>\nWe'll be back shortly!")
        if FORCE_CHANNEL and not (isinstance(event, CallbackQuery) and event.data == "chk"):
            if not await joined(data["bot"], u.id):
                url = f"https://t.me/{FORCE_CHANNEL.lstrip('@')}"
                return await deny(f"{E('lock')} <b>Please join our channel first</b>\nJoin, then tap <b>I've Joined</b>.",
                                  kb([B("Join Channel", url=url, key="join", fb="📢")],
                                     [B("I've Joined", "chk", "join")]))
        await refresh_menu(event, u, row)
        return await handler(event, data)


class PermMW(BaseMiddleware):
    """Blocks admin callbacks the sub-admin has no permission for."""

    async def __call__(self, handler, event, data):
        parts = (getattr(event, "data", "") or "").split(":")
        if len(parts) >= 2 and parts[0] == "adm":
            perm = OP_PERM.get(parts[1])
            if perm and not can(event.from_user.id, perm):
                return await event.answer("⛔ You don't have permission for this.", show_alert=True)
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
         f"{E('id')} <b>ID:</b> <code>{uid}</code>\n"
         f"{E('name')} <b>Name:</b> {esc(u.get('full_name') or '-')}\n"
         f"{E('link')} <b>Username:</b> {un}\n{BAR}\n"
         f"{E('wallet')} <b>Balance:</b> {money(u['balance'])}\n"
         f"{E('cart')} <b>Total Spent:</b> {money(u['spent'])}\n"
         f"{E('money')} <b>Total Deposited:</b> {money(dep)}\n"
         f"{E('orders')} <b>Orders:</b> {n}\n{BAR}\n"
         f"{E('rank')} <b>Rank:</b> {rank_of(u['spent'])}\n"
         f"{E('date')} <b>Member since:</b> {fdate(u['joined_at'])}")
    await show(ev, t, kb([B("My Orders", "ords:0", "my_orders"), B("Add Funds", "dep", "deposit")],
                         [B("Transactions", "txh:0", "history")]))


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
    btns = [[B(n + (" (Auto)" if is_auto(n) else ""), f"depm:{i}", "pay")] for i, (n, _) in enumerate(methods)]
    btns.append([B("Cancel", "close", "cancel")])
    await show(ev, f"{E('money')} <b>Add Funds</b>\n\nChoose a payment method 👇\n"
                   f"<i>(Minimum: {money(min_deposit())})</i>", kb(*btns))


# ═════════════ MENU ROUTER (bottom buttons always win over FSM states) ═════════════
menu = Router()
STAFF = F.from_user.id.func(is_staff)


@menu.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    ADMIN_MODE.discard(m.from_user.id)
    await m.answer(home_text(m.from_user.id), reply_markup=menu_kb(m.from_user.id))


@menu.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(home_text(m.from_user.id), reply_markup=menu_kb(m.from_user.id))


@menu.message(Command("admin"), STAFF)
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    ADMIN_MODE.add(m.from_user.id)
    await m.answer(f"{E('crown')} <b>Admin Panel</b>\n<i>Use the bottom buttons to manage your store.</i>",
                   reply_markup=admin_menu_kb(m.from_user.id))


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
    elif k == "history":
        await screen_history(m, m.from_user.id)
    elif k == "code":
        await screen_code(m)
    elif k == "support":
        await screen_support(m)
    elif k == "admin" and is_staff(m.from_user.id):
        ADMIN_MODE.add(m.from_user.id)
        await m.answer(f"{E('crown')} <b>Admin Panel</b>\n<i>Use the bottom buttons to manage your store.</i>",
                       reply_markup=admin_menu_kb(m.from_user.id))


@menu.message(F.text.func(lambda t: menu_key(t) in ADMIN_KEYS), STAFF)
async def on_admin_menu(m: Message, state: FSMContext):
    await state.clear()
    k, uid = menu_key(m.text), m.from_user.id
    ADMIN_MODE.add(uid)
    if not can(uid, KEY_PERM[k]):
        return await m.answer("⛔ You don't have permission for this.", reply_markup=admin_menu_kb(uid))
    if k == "a_exit":
        ADMIN_MODE.discard(uid)
        await m.answer("🏠 Back to user menu.", reply_markup=user_menu_kb(uid))
    elif k == "a_dash":
        await screen_stats(m)
    elif k == "a_report":
        await screen_report(m, 7)
    elif k == "a_users":
        await screen_users(m, 0)
    elif k == "a_prod":
        await screen_aprods(m, 0)
    elif k == "a_cat":
        await screen_acats(m)
    elif k == "a_deps":
        await screen_adeps(m)
    elif k == "a_bc":
        await screen_bc_audience(m)
    elif k == "a_notice":
        await state.set_state(NoticeS.text)
        await m.answer(f"{E('notice')} <b>Notice Board</b>\n\nCurrent:\n{SET.get('notice') or '<i>(none)</i>'}\n\n"
                       "Send the new notice (formatting &amp; custom emoji supported).\nSend <code>clear</code> to remove it.",
                       reply_markup=kb([B("Cancel", "close", "x_no")]))
    elif k == "a_set":
        await screen_settings(m)
    elif k == "a_subs":
        await screen_subs(m)
    elif k == "a_style":
        await screen_styles(m, 0)
    elif k == "a_emoji":
        await screen_emojis(m, 0)


# ═════════════ USER ROUTER ═════════════
router = Router()


class Dep(StatesGroup):
    amount = State()
    trx = State()
    ss = State()


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


@router.callback_query(F.data.startswith("txh:"))
async def cb_txh(c: CallbackQuery):
    await screen_history(c, c.from_user.id, int(c.data.split(":")[1]))


@router.callback_query(F.data.startswith("p:"))
async def cb_product(c: CallbackQuery):
    p = product_row(int(c.data.split(":")[1]))
    if not p or not p["active"]:
        return await c.answer("Product not available.", show_alert=True)
    t = (f"{E('stock')} <b>{esc(p['name'])}</b>\n\n{desc_html(p)}\n\n"
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
        await log_txn(uid, "purchase", -p["price"], f"#{oid} {p['name']}", bal=u["balance"])
    head = f"{E('party')} <b>{esc(p['name'])} purchase successful, Now enjoy!</b> {E('ok')}"
    await show(c, invoice_text(o, head), kb([B("My Orders", "ords:0", "my_orders"), B("Buy More", "buy", "buy")]))
    await notify_admins(c.bot, f"{E('cart')} <b>New order #{oid}</b>\nUser: <code>{uid}</code> "
                               f"(@{esc(c.from_user.username or '-')})\nProduct: {esc(p['name'])} · {money(p['price'])}",
                        perm="dash")
    await check_low(c.bot, pid)


@router.callback_query(F.data.startswith("o:"))
async def cb_order(c: CallbackQuery):
    o = D["orders"].get(c.data.split(":")[1])
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Order not found.", show_alert=True)
    await show(c, invoice_text(o), kb([B("Back", "ords:0", "back")]))


# ── Deposit flow ──
@router.callback_query(F.data.startswith("depm:"))
async def cb_depm(c: CallbackQuery, state: FSMContext):
    idx, methods = int(c.data.split(":")[1]), pay_methods()
    if idx >= len(methods):
        return await c.answer("Method not found.", show_alert=True)
    name, details = methods[idx]
    await state.clear()
    if is_auto(name):                                   # Binance Pay -> verified automatically
        await state.set_state(Dep.trx)
        await state.update_data(method=name, auto=True, tries=0)
        await show(c, f"{E('binance')} <b>{esc(name)}</b>  <i>({E('auto')} auto verified)</i>\n\n"
                      f"<code>{esc(details)}</code>\n\n"
                      f"1️⃣ Send <b>USDT</b> to the above Binance Pay account.\n"
                      f"2️⃣ Send the <b>Order ID</b> (Transaction ID) of your payment here.\n\n"
                      f"{E('money')} Rate: <b>1 USDT = {money(usd_rate())}</b>\n"
                      f"{E('bolt')} Balance is added instantly after verification.",
                   kb([B("Cancel", "close", "cancel")]))
        return
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


async def auto_deposit(m: Message, state: FSMContext, d: dict, trx: str):
    wait = await m.answer(f"{E('pending')} Verifying your payment with Binance…")
    st, tx = await binance_find(trx)
    if st == "error":
        await state.clear()
        await notify_admins(m.bot, f"{E('low')} <b>Binance verification failed</b> (API error) for user "
                                   f"<code>{m.from_user.id}</code>, Order ID <code>{esc(trx)}</code>. "
                                   f"Check API keys / IP restriction / region.")
        return await wait.edit_text(f"{E('no')} Verification is temporarily unavailable.\n"
                                    "Please try again in a few minutes or contact support.")
    if st == "notfound":
        tries = int(d.get("tries", 0)) + 1
        if tries >= 5:
            await state.clear()
            return await wait.edit_text(f"{E('no')} Payment not found after several tries.\n"
                                        "Please check the Order ID or contact support.")
        await state.update_data(tries=tries)
        return await wait.edit_text(f"{E('no')} <b>Payment not found yet.</b>\nBinance can take a minute — "
                                    f"check the Order ID and send it again. <i>({tries}/5)</i>",
                                    reply_markup=kb([B("Cancel", "close", "cancel")]))
    try:
        amt = float(tx.get("amount", 0))
    except (TypeError, ValueError):
        amt = 0.0
    cur = str(tx.get("currency", "")).upper()
    if amt <= 0:
        return await wait.edit_text(f"{E('no')} This is not an incoming payment.")
    if cur not in STABLE:
        await state.clear()
        return await wait.edit_text(f"{E('no')} Unsupported currency <b>{esc(cur)}</b>. Please pay in USDT "
                                    "or contact support.")
    credit = round(amt * usd_rate(), 2)
    async with dep_lock:
        if trx_used(trx):
            await state.clear()
            return await wait.edit_text(f"{E('no')} This Transaction ID was <b>already used</b>.")
        did = await nid("deposits")
        D["deposits"][str(did)] = {"id": did, "user_id": m.from_user.id, "method": d.get("method", "Binance Pay"),
                                   "amount": credit, "trx": trx, "status": "approved", "auto": 1,
                                   "raw": f"{amt:g} {cur}", "created_at": int(time.time())}
        u = get_user(m.from_user.id)
        u["balance"] += credit
        bal = u["balance"]
        await put("deposits", did)
        await put("users", m.from_user.id)
        await log_txn(m.from_user.id, "deposit", credit, f"Binance auto · #{did}", bal=bal)
    await state.clear()
    await wait.edit_text(f"{E('ok')} <b>Deposit successful!</b>\n\nReceived: <b>{amt:g} {esc(cur)}</b>\n"
                         f"Credited: <b>{money(credit)}</b>\nNew balance: <b>{money(bal)}</b>",
                         reply_markup=kb([B("Buy Product", "buy", "buy")]))
    await notify_admins(m.bot, f"{E('auto')} <b>Auto deposit #{did}</b>\nUser: <code>{m.from_user.id}</code> "
                               f"(@{esc(m.from_user.username or '-')})\n{amt:g} {esc(cur)} → <b>{money(credit)}</b>\n"
                               f"Order ID: <code>{esc(trx)}</code>", perm="deps")


@router.message(StateFilter(Dep.trx))
async def dep_trx(m: Message, state: FSMContext):
    d, trx = await state.get_data(), (m.text or "").strip()
    if len(trx) < 4 or len(trx) > 120:
        return await m.answer("❗ Please send a valid Transaction ID (text).")
    if trx_used(trx):
        return await m.answer(f"{E('no')} <b>This Transaction ID was already submitted.</b>\n"
                              "Each ID can only be used once. Send a different one or /cancel")
    if d.get("auto"):
        return await auto_deposit(m, state, d, trx)
    await state.update_data(trx=trx)
    await state.set_state(Dep.ss)
    await m.answer(f"{E('photo')} <b>Payment screenshot</b> <i>(optional)</i>\n\n"
                   "Send a screenshot of your payment to help us verify faster, or tap <b>Skip</b>.\n"
                   "<i>Send it as a photo, not as a file.</i>",
                   reply_markup=kb([B("Skip &amp; Submit".replace("&amp;", "&"), "depskip", "skip")],
                                   [B("Cancel", "close", "cancel")]))


async def finish_manual(bot: Bot, user, chat_msg: Message, state: FSMContext, ss=None):
    d = await state.get_data()
    if not d.get("trx"):
        await state.clear()
        return await chat_msg.answer("Session expired. Please start the deposit again.")
    async with dep_lock:
        if trx_used(d["trx"]):
            await state.clear()
            return await chat_msg.answer(f"{E('no')} This Transaction ID was <b>already submitted</b>.")
        did = await nid("deposits")
        D["deposits"][str(did)] = {"id": did, "user_id": user.id, "method": d["method"], "amount": d["amount"],
                                   "trx": d["trx"], "status": "pending", "created_at": int(time.time()),
                                   "ss": ss[0] if ss else "", "ss_uid": ss[1] if ss else ""}
        await put("deposits", did)
    await state.clear()
    await chat_msg.answer(f"{E('ok')} <b>Deposit request submitted!</b>\n\nRequest: <b>#{did}</b>\n"
                          f"Method: {esc(d['method'])}\nAmount: {money(d['amount'])}\n"
                          f"Trx: <code>{esc(d['trx'])}</code>\n\n"
                          f"{E('pending')} Our team will verify it shortly. You'll be notified once approved.")
    await notify_admins(
        bot, f"{E('money')} <b>New deposit #{did}</b>\nUser: <code>{user.id}</code> "
             f"(@{esc(user.username or '-')})\nMethod: {esc(d['method'])}\n"
             f"Amount: <b>{money(d['amount'])}</b>\nTrx: <code>{esc(d['trx'])}</code>\n"
             f"Screenshot: {'yes' if ss else 'no'}",
        kb([B("Approve", f"adm:dok:{did}", "x_ok"), B("Reject", f"adm:dno:{did}", "x_no")]),
        perm="deps", photo=ss[0] if ss else None)


@router.message(StateFilter(Dep.ss), F.photo)
async def dep_ss(m: Message, state: FSMContext):
    ph = m.photo[-1]
    if ss_used(ph.file_unique_id):
        return await m.answer(f"{E('no')} This screenshot was <b>already submitted</b> before. "
                              "Send a different one or tap Skip.")
    await finish_manual(m.bot, m.from_user, m, state, (ph.file_id, ph.file_unique_id))


@router.message(StateFilter(Dep.ss))
async def dep_ss_other(m: Message, state: FSMContext):
    await m.answer("📷 Please send a <b>photo</b> (screenshot) or tap <b>Skip</b> below.")


@router.callback_query(F.data == "depskip", StateFilter(Dep.ss))
async def dep_skip(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await finish_manual(c.bot, c.from_user, c.message, state, None)


# ═════════════ ADMIN ROUTER ═════════════
admin = Router()
admin.message.filter(STAFF)
admin.callback_query.filter(STAFF)
admin.callback_query.middleware(PermMW())


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
class EditDesc(StatesGroup):
    text = State()
class FindUser(StatesGroup):
    query = State()
class Adjust(StatesGroup):
    amount = State()
class Broadcast(StatesGroup):
    msg = State()
class BcIds(StatesGroup):
    ids = State()
class NoticeS(StatesGroup):
    text = State()
class SetVal(StatesGroup):
    value = State()
class EmojiS(StatesGroup):
    value = State()
class SubAdd(StatesGroup):
    uid = State()


# ── Dashboard ──
async def screen_stats(ev):
    day = int(time.time()) - 86400
    users, orders, deps = D["users"].values(), D["orders"].values(), D["deposits"].values()
    t = (f"{E('stats')} <b>Dashboard</b>\n{BAR}\n"
         f"{E('users')} Users: <b>{len(D['users'])}</b> (+{sum(1 for u in users if u['joined_at'] > day)} today)\n"
         f"{E('stock')} Products: <b>{len(D['products'])}</b> · Stock left: "
         f"<b>{sum(1 for s in D['stock'].values() if not s['sold'])}</b>\n"
         f"{E('low')} Low-stock products: <b>{len(low_list())}</b>\n"
         f"{E('orders')} Orders: <b>{len(D['orders'])}</b>\n"
         f"{E('cart')} Revenue: <b>{money(sum(o['price'] for o in orders))}</b> "
         f"(24h: {money(sum(o['price'] for o in orders if o['created_at'] > day))})\n"
         f"{E('money')} Deposits approved: <b>{money(sum(d['amount'] for d in deps if d['status'] == 'approved'))}</b>\n"
         f"{E('pending')} Pending deposits: <b>{sum(1 for d in deps if d['status'] == 'pending')}</b>\n"
         f"{E('wallet')} Users' total balance: <b>{money(sum(u['balance'] for u in users))}</b>")
    await show(ev, t, kb([B("Refresh", "adm:stats", "x_refresh"), B("Low Stock", "adm:low", "x_low")],
                         [B("Sales Report", "adm:rep:7", "a_report")]))


@admin.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    await screen_stats(c)


# ── Low stock ──
async def screen_low(ev):
    rows = low_list()
    th = low_threshold()
    if not rows:
        return await show(ev, f"{E('low')} <b>Low Stock</b>\n\n<i>All active products have more than {th} items 🎉</i>",
                          kb([B("Back", "adm:stats", "x_nav")]))
    btns = [[B(f"{p['name']} · {p['stk']} left", f"adm:p:{p['id']}", "x_item", cid=p.get("emoji"), fb="")]
            for p in rows[:25]]
    btns.append([B("Back", "adm:stats", "x_nav")])
    await show(ev, f"{E('low')} <b>Low Stock</b>  <i>(≤ {th} items)</i>\nTap a product to add stock 👇", kb(*btns))


@admin.callback_query(F.data == "adm:low")
async def adm_low(c: CallbackQuery):
    await screen_low(c)


# ── Sales report ──
PERIODS = {1: "24h", 7: "7 days", 30: "30 days", 0: "All time"}


def in_period(ts, days) -> bool:
    return days == 0 or int(ts) >= int(time.time()) - days * 86400


async def screen_report(ev, days=7):
    days = days if days in PERIODS else 7
    orders = [o for o in D["orders"].values() if in_period(o["created_at"], days)]
    deps = [d for d in D["deposits"].values() if d["status"] == "approved" and in_period(d["created_at"], days)]
    new_users = sum(1 for u in D["users"].values() if in_period(u["joined_at"], days))
    rev = sum(o["price"] for o in orders)
    prods, buyers = {}, {}
    for o in orders:
        row = prods.setdefault(o["product_name"], [0, 0.0])
        row[0] += 1
        row[1] += o["price"]
        buyers[o["user_id"]] = buyers.get(o["user_id"], 0.0) + o["price"]
    top_p = sorted(prods.items(), key=lambda x: -x[1][1])[:5]
    top_b = sorted(buyers.items(), key=lambda x: -x[1])[:5]
    t = (f"{E('report')} <b>Sales Report</b> — {PERIODS[days]}\n{BAR}\n"
         f"{E('orders')} Orders: <b>{len(orders)}</b>\n"
         f"{E('cart')} Revenue: <b>{money(rev)}</b>\n"
         f"{E('stats')} Avg order: <b>{money(rev / len(orders) if orders else 0)}</b>\n"
         f"{E('money')} Deposits: <b>{money(sum(d['amount'] for d in deps))}</b> ({len(deps)})\n"
         f"{E('users')} New users: <b>{new_users}</b>\n")
    if top_p:
        t += f"\n{E('star')} <b>Top products</b>\n"
        for i, (n, (cnt, r)) in enumerate(top_p, 1):
            t += f"{i}. {esc(n)} — {cnt} sold · {money(r)}\n"
    if top_b:
        t += f"\n{E('crown')} <b>Top buyers</b>\n"
        for i, (uid, r) in enumerate(top_b, 1):
            u = get_user(uid) or {}
            t += f"{i}. {esc((u.get('full_name') or '-')[:20])} (<code>{uid}</code>) — {money(r)}\n"
    if not orders:
        t += "\n<i>No sales in this period.</i>"
    per = [B(("• " if k == days else "") + lab, f"adm:rep:{k}", "x_period", fb="") for k, lab in PERIODS.items()]
    await show(ev, t, kb(per, [B("Export CSV", f"adm:repcsv:{days}", "x_csv"), B("Refresh", f"adm:rep:{days}", "x_refresh")]))


@admin.callback_query(F.data.regexp(r"^adm:rep:\d+$"))
async def adm_rep(c: CallbackQuery):
    await screen_report(c, int(c.data.split(":")[2]))


@admin.callback_query(F.data.startswith("adm:repcsv:"))
async def adm_repcsv(c: CallbackQuery):
    days = int(c.data.split(":")[2])
    tag = "all" if days == 0 else f"{days}d"
    ob = io.StringIO()
    w = csv.writer(ob)
    w.writerow(["order_id", "date", "user_id", "username", "product", "price"])
    for o in sorted((o for o in D["orders"].values() if in_period(o["created_at"], days)), key=lambda o: o["id"]):
        w.writerow([o["id"], fdate(o["created_at"]), o["user_id"],
                    (get_user(o["user_id"]) or {}).get("username", ""), o["product_name"], o["price"]])
    await c.message.answer_document(BufferedInputFile(ob.getvalue().encode("utf-8-sig"), filename=f"orders_{tag}.csv"))
    ob = io.StringIO()
    w = csv.writer(ob)
    w.writerow(["deposit_id", "date", "user_id", "method", "amount", "trx", "status", "auto"])
    for d in sorted((d for d in D["deposits"].values() if in_period(d["created_at"], days)), key=lambda d: d["id"]):
        w.writerow([d["id"], fdate(d["created_at"]), d["user_id"], d["method"], d["amount"], d["trx"],
                    d["status"], d.get("auto", 0)])
    await c.message.answer_document(BufferedInputFile(ob.getvalue().encode("utf-8-sig"), filename=f"deposits_{tag}.csv"))
    await c.answer("Exported ✅")


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
    await state.update_data(name=(m.text or "").strip())
    await state.set_state(AddP.desc)
    await m.answer("📝 Send the <b>description</b> (features, validity, region…)\n<i>Premium/custom emoji and bold/italic formatting are kept as you send them.</i>")


@admin.message(StateFilter(AddP.desc))
async def addp_desc(m: Message, state: FSMContext):
    await state.update_data(desc=m.html_text.strip())
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
                               "price": price, "active": 1, "emoji": "", "dhtml": 1}
    await put("products", pid)
    await state.clear()
    await m.answer(f"{E('ok')} Product created!", reply_markup=kb(
        [B("Add / Import Stock", f"adm:stk:{pid}", "x_import"), B("Set Emoji", f"adm:pem:{pid}", "x_edit", fb="⭐")],
        [B("Products", "adm:pl:0", "x_nav")]))


async def render_admin_product(c: CallbackQuery, pid: int):
    p = product_row(pid)
    if not p:
        return await c.answer("Not found", show_alert=True)
    sold = sum(1 for s in D["stock"].values() if int(s["pid"]) == pid and s["sold"])
    t = (f"{E('stock')} <b>#{p['id']} {esc(p['name'])}</b>\nCategory: <b>{esc(cat_name(p['category']))}</b>\n\n"
         f"{desc_html(p)}\n\nPrice: <b>{money(p['price'])}</b>\n"
         f"In stock: <b>{p['stk']}</b> · Sold: <b>{sold}</b>\n"
         f"Emoji: <code>{p.get('emoji') or 'none'}</code>\n"
         f"Status: {'🟢 Active' if p['active'] else '🔴 Hidden'}")
    await show(c, t, kb(
        [B("Add / Import Stock", f"adm:stk:{pid}", "x_import"), B("Edit Price", f"adm:ep:{pid}", "x_edit")],
        [B("Edit Description", f"adm:ed:{pid}", "x_edit", fb="📝"), B("Set Emoji", f"adm:pem:{pid}", "x_edit", fb="⭐")],
        [B("Hide" if p["active"] else "Show", f"adm:tg:{pid}", "x_edit", fb="👁")],
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
    LOW_SENT.pop(pid, None)
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


@admin.callback_query(F.data.startswith("adm:ed:"))
async def adm_ed(c: CallbackQuery, state: FSMContext):
    await state.set_state(EditDesc.text)
    await state.update_data(pid=int(c.data.split(":")[2]))
    await show(c, "📝 Send the <b>new description</b>.\n<i>Premium/custom emoji and formatting are kept.</i>",
               kb([B("Cancel", "close", "x_no")]))


@admin.message(StateFilter(EditDesc.text))
async def ed_save(m: Message, state: FSMContext):
    d = await state.get_data()
    p = prod(d["pid"])
    if p:
        p["description"], p["dhtml"] = m.html_text.strip(), 1
        await put("products", d["pid"])
    await state.clear()
    await m.answer(f"{E('ok')} Description updated.", reply_markup=kb([B("Back to product", f"adm:p:{d['pid']}", "x_nav")]))


# ── Stock add / import ──
@admin.callback_query(F.data.startswith("adm:stk:"))
async def adm_stk(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddStock.lines)
    await state.update_data(pid=int(c.data.split(":")[2]))
    await show(c, f"{E('import')} <b>Add / Import Stock</b>\n\n"
                  "Send items as <b>text</b> (one per line) <b>or upload a .txt / .csv file</b> (one item per line).\n"
                  "For multi-line items, separate them with a line containing only <code>---</code>.\n"
                  "<i>Duplicates (already in stock or sold) are skipped automatically.</i>\n\n"
                  "<i>Example:</i>\n<code>1.2.3.4:8080:user:pass\n5.6.7.8:8080:user:pass</code>",
               kb([B("Cancel", "close", "x_no")]))


def parse_stock(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()
    if re.search(r"\n-{3,}\n", text):
        items = [x.strip() for x in re.split(r"\n-{3,}\n", text)]
    else:
        items = [x.strip() for x in text.splitlines()]
    return [x for x in items if x]


@admin.message(StateFilter(AddStock.lines))
async def stk_save(m: Message, state: FSMContext):
    d = await state.get_data()
    pid = d["pid"]
    if m.document:
        if (m.document.file_size or 0) > 5 * 1024 * 1024:
            return await m.answer("❗ File too large (max 5 MB).")
        buf = io.BytesIO()
        await m.bot.download(m.document, destination=buf)
        text = buf.getvalue().decode("utf-8-sig", errors="replace")
    else:
        text = (m.text or "").strip()
    items = parse_stock(text)
    if not items:
        return await m.answer("❗ Nothing to add.")
    have = {s["data"].strip() for s in D["stock"].values() if int(s["pid"]) == pid}
    fresh, dup = [], 0
    for x in items:
        if x in have:
            dup += 1
        else:
            have.add(x)
            fresh.append(x)
    if fresh:
        ids = await nids("stock", len(fresh))
        for sid, x in zip(ids, fresh):
            D["stock"][str(sid)] = {"pid": pid, "data": x, "sold": 0}
        await put("stock", *ids)
    await state.clear()
    await m.answer(f"{E('ok')} Added <b>{len(fresh)}</b> items to stock."
                   + (f"\n{E('low')} Skipped <b>{dup}</b> duplicate(s)." if dup else ""),
                   reply_markup=kb([B("Back to product", f"adm:p:{pid}", "x_nav")]))
    await check_low(m.bot, pid)


# ── Deposits ──
async def screen_adeps(ev):
    rows = sorted((d for d in D["deposits"].values() if d["status"] == "pending"), key=lambda d: d["id"])[:20]
    hist = [B("History", "adm:dh:0", "x_hist")]
    if not rows:
        return await show(ev, f"{E('money')} <b>Pending Deposits</b>\n\n<i>Nothing pending 🎉</i>", kb(hist))
    btns = [[B(f"#{d['id']} · {money(d['amount'])} · {d['method']} · {d['user_id']}" + (" 🖼" if d.get("ss") else ""),
               f"adm:d:{d['id']}", "x_item", fb="⏳")] for d in rows]
    btns.append(hist)
    await show(ev, f"{E('money')} <b>Pending Deposits</b>", kb(*btns))


@admin.callback_query(F.data == "adm:deps")
async def adm_deps(c: CallbackQuery):
    await screen_adeps(c)


@admin.callback_query(F.data.startswith("adm:dh:"))
async def adm_dh(c: CallbackQuery):
    rows = sorted(D["deposits"].values(), key=lambda d: -d["id"])
    items, page, pages = paginate(rows, int(c.data.split(":")[2]), 10)
    mark = {"approved": "🟢", "pending": "⏳", "rejected": "🔴"}
    btns = [[B(f"{mark.get(d['status'], '▫️')} #{d['id']} · {money(d['amount'])} · {d['method']} · {d['user_id']}",
               f"adm:d:{d['id']}", "x_item", fb="")] for d in items]
    nr = nav_row("adm:dh", page, pages)
    if nr:
        btns.append(nr)
    btns.append([B("Back", "adm:deps", "x_nav")])
    await show(c, f"{E('hist')} <b>Deposit History</b>  <i>({len(rows)} total)</i>", kb(*btns))


@admin.callback_query(F.data.startswith("adm:d:"))
async def adm_dep(c: CallbackQuery):
    d = D["deposits"].get(c.data.split(":")[2])
    if not d:
        return await c.answer("Not found", show_alert=True)
    t = (f"{E('money')} <b>Deposit #{d['id']}</b>\nUser: <code>{d['user_id']}</code>\nMethod: {esc(d['method'])}\n"
         f"Amount: <b>{money(d['amount'])}</b>" + (f" ({esc(d['raw'])})" if d.get("raw") else "") + "\n"
         f"Trx: <code>{esc(d['trx'])}</code>\nStatus: <b>{d['status']}</b>"
         + (" · auto" if d.get("auto") else "") + f"\nDate: {fdate(d['created_at'])}")
    rows = []
    if d["status"] == "pending":
        rows.append([B("Approve", f"adm:dok:{d['id']}", "x_ok"), B("Reject", f"adm:dno:{d['id']}", "x_no")])
    if d.get("ss"):
        rows.append([B("View Screenshot", f"adm:dss:{d['id']}", "x_shot")])
    rows.append([B("Back", "adm:deps", "x_nav")])
    await show(c, t, kb(*rows))


@admin.callback_query(F.data.startswith("adm:dss:"))
async def adm_dss(c: CallbackQuery):
    d = D["deposits"].get(c.data.split(":")[2])
    if not d or not d.get("ss"):
        return await c.answer("No screenshot.", show_alert=True)
    await c.message.answer_photo(d["ss"], caption=f"{E('photo')} Screenshot of deposit #{d['id']}")
    await c.answer()


@admin.callback_query(F.data.startswith("adm:dok:"))
async def adm_dok(c: CallbackQuery):
    did = c.data.split(":")[2]
    async with buy_lock:
        d = D["deposits"].get(did)
        if not d or d["status"] != "pending":
            return await c.answer("Already handled.", show_alert=True)
        d["status"] = "approved"
        d["by"] = c.from_user.id
        u = get_user(d["user_id"])
        bal = None
        if u:
            u["balance"] += d["amount"]
            bal = u["balance"]
            await put("users", d["user_id"])
        await put("deposits", did)
        if u:
            await log_txn(d["user_id"], "deposit", d["amount"], f"{d['method']} · #{did}", bal=bal)
    await stamp(c, f"\n\n{E('ok')} <b>Approved by {esc(c.from_user.full_name)}</b>")
    await c.answer("Approved ✅")
    try:
        await c.bot.send_message(
            d["user_id"], f"{E('ok')} <b>Deposit Approved!</b>\nAmount: <b>{money(d['amount'])}</b>\n"
                          f"New balance: <b>{money(bal if bal is not None else 0)}</b>",
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
    d["by"] = c.from_user.id
    await put("deposits", did)
    await stamp(c, f"\n\n{E('no')} <b>Rejected by {esc(c.from_user.full_name)}</b>")
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
    role = "Owner" if is_owner(uid) else ("Sub admin" if str(uid) in D["subs"] else "User")
    t = (f"{E('profile')} <b>{esc(u.get('full_name') or '-')}</b>\n{BAR}\n"
         f"{E('id')} <code>{uid}</code> · {un}\n{E('wallet')} Balance: <b>{money(u['balance'])}</b>\n"
         f"{E('cart')} Spent: <b>{money(u['spent'])}</b>\n{E('orders')} Orders: <b>{n}</b>\n"
         f"{E('rank')} Rank: {rank_of(u['spent'])}\n{E('date')} Joined: {fdate(u['joined_at'])}\n"
         f"Role: <b>{role}</b>\n"
         f"Status: {'🚫 Banned' if u['banned'] else '✅ Active'}")
    return t, kb(
        [B("Add Balance", f"adm:adj:{uid}:+", "x_ok", fb="➕"), B("Deduct", f"adm:adj:{uid}:-", "x_no", fb="➖")],
        [B("Orders", f"adm:uo:{uid}", "x_edit", fb="📦"), B("Transactions", f"adm:ut:{uid}:0", "x_hist")],
        [B("Unban" if u["banned"] else "Ban", f"adm:ban:{uid}", "x_no", fb="🚫")],
        [B("Users", "adm:ul:0", "x_nav")])


@admin.callback_query(F.data.regexp(r"^adm:u:\d+$"))
async def adm_user(c: CallbackQuery, state: FSMContext):
    await state.clear()
    t, mk = user_panel(int(c.data.split(":")[2]))
    if not t:
        return await c.answer("Not found", show_alert=True)
    await show(c, t, mk)


@admin.callback_query(F.data.startswith("adm:ut:"))
async def adm_ut(c: CallbackQuery):
    _, _, uid, page = c.data.split(":")
    await screen_history(c, int(uid), int(page), prefix=f"adm:ut:{uid}", back=B("Back", f"adm:u:{uid}", "x_nav"))


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
    if is_staff(uid) and not is_owner(c.from_user.id):
        return await c.answer("Only owners can ban staff.", show_alert=True)
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
    u = get_user(d["uid"])
    u["balance"] += delta
    await put("users", d["uid"])
    await log_txn(d["uid"], "admin_add" if delta >= 0 else "admin_deduct", delta,
                  f"by {m.from_user.full_name}", bal=u["balance"])
    await state.clear()
    t, mk = user_panel(d["uid"])
    await m.answer(f"{E('ok')} Balance updated.\n\n{t}", reply_markup=mk)
    try:
        await m.bot.send_message(d["uid"], f"{E('wallet')} Your balance was adjusted by "
                                           f"<b>{'+' if delta >= 0 else '-'}{money(abs(delta))}</b> by admin.")
    except Exception:
        pass


# ── Targeted broadcast ──
AUDS = {"all": "All users", "buyers": "Buyers", "nonbuyers": "Never bought", "balance": "Has balance",
        "active": "Active (7d)", "new": "New (7d)"}


def aud_ids(kind: str, ids=None) -> list[int]:
    wk = int(time.time()) - 7 * 86400
    buyers = {o["user_id"] for o in D["orders"].values()}
    out = []
    for u in D["users"].values():
        if u["banned"]:
            continue
        uid = u["id"]
        if kind == "all":
            ok = True
        elif kind == "buyers":
            ok = uid in buyers
        elif kind == "nonbuyers":
            ok = uid not in buyers
        elif kind == "balance":
            ok = u["balance"] > 0
        elif kind == "active":
            ok = int(u.get("seen", u["joined_at"])) >= wk
        elif kind == "new":
            ok = u["joined_at"] >= wk
        elif kind == "ids":
            ok = uid in (ids or [])
        else:
            ok = False
        if ok:
            out.append(uid)
    return out


async def screen_bc_audience(ev):
    btns, row = [], []
    for k, label in AUDS.items():
        row.append(B(f"{label} · {len(aud_ids(k))}", f"adm:bca:{k}", "x_aud"))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([B("By User IDs", "adm:bca:ids", "x_aud")])
    btns.append([B("Cancel", "close", "x_no")])
    await show(ev, f"{E('target')} <b>Broadcast — choose audience</b>\n\nBanned users are always excluded.", kb(*btns))


@admin.callback_query(F.data.startswith("adm:bca:"))
async def adm_bca(c: CallbackQuery, state: FSMContext):
    kind = c.data.split(":")[2]
    if kind == "ids":
        await state.set_state(BcIds.ids)
        return await show(c, f"{E('target')} Send the <b>user IDs</b> (separated by space, comma or new line):",
                          kb([B("Cancel", "close", "x_no")]))
    if kind not in AUDS:
        return await c.answer("Unknown audience", show_alert=True)
    await state.set_state(Broadcast.msg)
    await state.update_data(aud=kind, ids=[])
    await show(c, f"{E('broadcast')} <b>Broadcast → {AUDS[kind]}</b> ({len(aud_ids(kind))} users)\n\n"
                  "Send the message to deliver.\n"
                  "<i>Text, photo, video, custom emoji - copied exactly as you send it.</i>",
               kb([B("Cancel", "close", "x_no")]))


@admin.message(StateFilter(BcIds.ids))
async def bc_ids(m: Message, state: FSMContext):
    ids = [int(x) for x in re.findall(r"\d+", m.text or "") if str(int(x)) in D["users"]]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return await m.answer("❗ No known user IDs found. Send again or /cancel")
    await state.set_state(Broadcast.msg)
    await state.update_data(aud="ids", ids=ids)
    await m.answer(f"{E('broadcast')} <b>Broadcast → {len(ids)} selected users</b>\n\nNow send the message to deliver.",
                   reply_markup=kb([B("Cancel", "close", "x_no")]))


@admin.message(StateFilter(Broadcast.msg))
async def bc_preview(m: Message, state: FSMContext):
    d = await state.get_data()
    n = len(aud_ids(d.get("aud", "all"), d.get("ids")))
    await state.update_data(chat=m.chat.id, mid=m.message_id)
    await m.answer(f"{E('broadcast')} Ready to send to <b>{n}</b> users "
                   f"(<i>{AUDS.get(d.get('aud', 'all'), 'Selected users')}</i>).\nPreview is the message above ☝️",
                   reply_markup=kb([B(f"Send to {n} users", "adm:bcgo", "x_ok", fb="📤")],
                                   [B("Cancel", "close", "x_no")]))


@admin.callback_query(F.data == "adm:bcgo")
async def bc_go(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("mid"):
        return await c.answer("Nothing to send.", show_alert=True)
    await state.clear()
    users = aud_ids(d.get("aud", "all"), d.get("ids"))
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
                "<code>bKash|017XXXXXXXX (Send Money)\nBinance Pay|XXXXXXXX</code>\n"
                "<i>Any method whose name contains \"binance\" is auto-verified (when API keys are set).</i>",
    "min_dep": f"Send the <b>minimum deposit</b> amount (number, {CURRENCY}).",
    "usd_rate": f"Send the <b>USDT → {CURRENCY} rate</b> (number). Used for Binance auto-deposit.",
    "low_stock": "Send the <b>low-stock limit</b> (number). Staff get an alert when a product has this many items or fewer.",
    "code_text": "Send the <b>Get Code</b> message text (HTML allowed). Send <code>clear</code> for default.",
    "code_url": "Send the <b>Get Code link</b> (https://… or @username). Send <code>clear</code> to remove.",
}
NUMERIC_SETTINGS = {"min_dep", "usd_rate", "low_stock"}


def onoff(key: str) -> str:
    return "ON 🟢" if flag(key) else "OFF 🔴"


async def screen_settings(ev):
    if not BINANCE_ON:
        auto = "not configured (set BINANCE_API_KEY / SECRET)"
    else:
        auto = onoff("auto_dep")
    t = (f"{E('settings')} <b>Settings</b>\n{BAR}\nSupport: <b>{esc(SET.get('support') or '-')}</b>\n"
         f"Min deposit: <b>{money(min_deposit())}</b>\nPayment methods: <b>{len(pay_methods())}</b>\n"
         f"Binance auto-deposit: <b>{auto}</b>\nUSDT rate: <b>{money(usd_rate())}</b>\n"
         f"Low-stock limit: <b>{low_threshold()}</b>\n"
         f"Get Code link: <b>{esc(SET.get('code_url') or '-')}</b>\n"
         f"Anti-spam: <b>{onoff('antispam')}</b>\n"
         f"Maintenance: <b>{'ON 🔴' if flag('maintenance') else 'OFF 🟢'}</b>")
    await show(ev, t, kb(
        [B("Support", "adm:sv:support", "x_edit", fb="💬"), B("Min Deposit", "adm:sv:min_dep", "x_edit", fb="💰")],
        [B("Payment Methods", "adm:sv:payments", "x_edit", fb="💳"), B("USDT Rate", "adm:sv:usd_rate", "x_edit", fb="💱")],
        [B("Low-Stock Limit", "adm:sv:low_stock", "x_edit", fb="⚠️")],
        [B("Code Text", "adm:sv:code_text", "x_edit", fb="🔑"), B("Code Link", "adm:sv:code_url", "x_edit", fb="🔗")],
        [B(f"Auto Deposit: {onoff('auto_dep')}", "adm:tgl:auto_dep", None, fb="", st="success" if flag("auto_dep") else "danger"),
         B(f"Anti-Spam: {onoff('antispam')}", "adm:tgl:antispam", None, fb="", st="success" if flag("antispam") else "danger")],
        [B("Maintenance: ON" if flag("maintenance") else "Maintenance: OFF", "adm:tgl:maintenance", None, fb="🛠",
           st="danger" if flag("maintenance") else None)]))


@admin.callback_query(F.data == "adm:set")
async def adm_set(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await screen_settings(c)


@admin.callback_query(F.data.startswith("adm:tgl:"))
async def adm_tgl(c: CallbackQuery):
    key = c.data.split(":")[2]
    if key not in TOGGLES:
        return await c.answer("Unknown", show_alert=True)
    await set_setting(key, "0" if flag(key) else "1")
    await screen_settings(c)


@admin.callback_query(F.data.startswith("adm:sv:"))
async def adm_sv(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    if key not in SETTING_PROMPTS:
        return await c.answer("Unknown", show_alert=True)
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
        if key in NUMERIC_SETTINGS:
            try:
                if float(val) < 0:
                    raise ValueError
            except ValueError:
                return await m.answer("❗ Send a valid number.")
        if key == "support":
            val = val.lstrip("@")
        await set_setting(key, val)
    await state.clear()
    await m.answer(f"{E('ok')} Saved.", reply_markup=kb([B("Settings", "adm:set", "x_nav")]))


# ── Sub admins (owners only) ──
def sub_panel(uid: int):
    s = D["subs"].get(str(uid))
    if not s:
        return None, None
    u = get_user(uid) or {}
    t = (f"{E('shield')} <b>Sub Admin</b>\n{BAR}\n{E('profile')} {esc(u.get('full_name') or '-')} · <code>{uid}</code>\n\n"
         "Tap to turn permissions on / off:\n<i>Colors, Custom Emoji and Sub Admins are owner-only.</i>")
    rows = []
    for k, label in PERMS.items():
        on = k in s.get("perms", [])
        rows.append([B(f"{'✅' if on else '⬜'} {label}", f"adm:sap:{uid}:{k}", None, fb="", st="success" if on else None)])
    rows.append([B("Remove sub admin", f"adm:sadel:{uid}", "x_no")])
    rows.append([B("Back", "adm:subs", "x_nav")])
    return t, kb(*rows)


async def screen_subs(ev):
    rows = sorted(D["subs"].values(), key=lambda s: s["id"])
    btns = []
    for s in rows:
        u = get_user(s["id"]) or {}
        btns.append([B(f"{(u.get('full_name') or '-')[:16]} · {s['id']} · {len(s.get('perms', []))} perms",
                       f"adm:sa:{s['id']}", "x_sub")])
    btns.append([B("Add Sub Admin", "adm:saadd", "x_add")])
    owners = ", ".join(f"<code>{i}</code>" for i in sorted(ADMIN_IDS)) or "-"
    await show(ev, f"{E('shield')} <b>Sub Admins</b>\n\nOwners: {owners}\nSub admins: <b>{len(rows)}</b>\n"
                   "<i>Sub admins get only the permissions you give them.</i>", kb(*btns))


@admin.callback_query(F.data == "adm:subs")
async def adm_subs(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await screen_subs(c)


@admin.callback_query(F.data == "adm:saadd")
async def adm_saadd(c: CallbackQuery, state: FSMContext):
    await state.set_state(SubAdd.uid)
    await show(c, f"{E('shield')} Send the <b>user ID</b> or <b>@username</b> of the new sub admin.\n"
                  "<i>The person must have started the bot at least once.</i>", kb([B("Cancel", "adm:subs", "x_no")]))


@admin.message(StateFilter(SubAdd.uid))
async def sa_add(m: Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        await state.clear()
        return
    q = (m.text or "").strip().lstrip("@")
    row = get_user(q) if q.isdigit() else next(
        (u for u in D["users"].values() if (u.get("username") or "").lower() == q.lower()), None)
    if not row:
        return await m.answer("❗ User not found (they must /start the bot first). Try again or /cancel")
    uid = row["id"]
    if is_owner(uid):
        return await m.answer("This user is already an owner.")
    if str(uid) not in D["subs"]:
        D["subs"][str(uid)] = {"id": uid, "perms": ["dash", "deps"], "added_at": int(time.time())}
        await put("subs", uid)
    await state.clear()
    t, mk = sub_panel(uid)
    await m.answer(f"{E('ok')} Sub admin added.\n\n{t}", reply_markup=mk)
    try:
        await m.bot.send_message(uid, f"{E('shield')} <b>You are now a sub admin.</b>\nOpen the Admin Panel from the menu.",
                                 reply_markup=user_menu_kb(uid))
    except Exception:
        pass


@admin.callback_query(F.data.regexp(r"^adm:sa:\d+$"))
async def adm_sa(c: CallbackQuery):
    t, mk = sub_panel(int(c.data.split(":")[2]))
    if not t:
        return await c.answer("Not found", show_alert=True)
    await show(c, t, mk)


@admin.callback_query(F.data.startswith("adm:sap:"))
async def adm_sap(c: CallbackQuery):
    _, _, uid, perm = c.data.split(":")
    uid = int(uid)
    s = D["subs"].get(str(uid))
    if not s or perm not in PERMS:
        return await c.answer("Not found", show_alert=True)
    perms = list(s.get("perms", []))
    perms.remove(perm) if perm in perms else perms.append(perm)
    s["perms"] = perms
    await put("subs", uid)
    t, mk = sub_panel(uid)
    await show(c, t, mk)
    try:   # refresh that person's admin keyboard
        await c.bot.send_message(uid, f"{E('shield')} Your admin permissions were updated.",
                                 reply_markup=menu_kb(uid))
    except Exception:
        pass


@admin.callback_query(F.data.startswith("adm:sadel:"))
async def adm_sadel(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    await show(c, "⚠️ <b>Remove this sub admin?</b>",
               kb([B("Yes, remove", f"adm:sadelok:{uid}", "x_no"), B("No", f"adm:sa:{uid}", "x_ok")]))


@admin.callback_query(F.data.startswith("adm:sadelok:"))
async def adm_sadelok(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    D["subs"].pop(str(uid), None)
    await put("subs", uid)
    ADMIN_MODE.discard(uid)
    await c.answer("Removed ✅")
    await screen_subs(c)
    try:
        await c.bot.send_message(uid, "Your sub-admin access was removed.", reply_markup=user_menu_kb(uid))
    except Exception:
        pass


# ── Button colors (user + admin buttons) ──
async def screen_styles(ev, page=0):
    keys = list(BTN)
    items, page, pages = paginate(keys, page, 10)
    btns = []
    for k in items:
        label, _, _, grp = BTN[k]
        btns.append([B(f"{label}  [{'User' if grp == 'user' else 'Admin'}] {STYLE_MARK[bstyle(k)]}", f"adm:stp:{k}", k)])
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


@admin.callback_query(F.data.startswith("adm:stp:"))      # (was adm:stk: - clashed with "Add Stock")
async def adm_stk_pick(c: CallbackQuery):
    k = c.data.split(":")[2]
    if k not in BTN: