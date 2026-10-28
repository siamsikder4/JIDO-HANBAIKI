# -*- coding: utf-8 -*-
"""
Your Proxy Shop Bot  (v3.2, Firebase storage + Telegram Account Selling)
------------------------------------------------------------------------
* Binance Pay API  -> Order ID verified automatically, balance added instantly
* bKash / Nagad / any other method -> admin approves (optional screenshot)
* Duplicate Transaction-ID + duplicate screenshot check
* Low-stock alert, transaction history, sales report + CSV
* Sub-admin roles, targeted broadcast, stock import (.txt/.csv)
* Anti-spam, custom emoji / button colors from Admin Panel

NEW in v3.2  -  SELL TELEGRAM ACCOUNTS
* Admin Panel -> "TG Accounts" -> Add Account:
      phone  ->  login code  ->  (2FA password)  ->  price
  The account is logged in INSIDE the bot (Telethon session saved in Firebase).
  Country flag + country name are detected automatically from the phone number.
* Users: bottom menu "Telegram Accounts" -> country (flag) -> number -> buy
* After purchase the user gets:  NUMBER  +  TWO-STEP password
  The user logs in on his own phone, then taps "Get Login Code" and the bot
  reads the code from the logged-in session and shows it inside the bot.
* "Login Done" button -> bot session is removed from the account (account becomes
  100% the buyer's).

Env vars: BOT_TOKEN, ADMIN_IDS, BRAND_NAME, CURRENCY, FORCE_CHANNEL,
          BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_HOST (optional),
          TG_API_ID, TG_API_HASH            (from https://my.telegram.org -> API development tools)
          FIREBASE_CRED_JSON (whole service-account JSON)  or  FIREBASE_CRED (file path)
Requires: aiogram >= 3.25, firebase-admin, python-dotenv, aiohttp, telethon
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
# ── Firebase (Firestore) ──  (prefer env var FIREBASE_CRED_JSON; never upload keys to GitHub)
FIREBASE_JSON = {
}
FIREBASE_CRED = os.getenv("FIREBASE_CRED", "firebase.json")
FIREBASE_CRED_JSON = os.getenv("FIREBASE_CRED_JSON", "").strip()
STORE_FILE = os.getenv("STORE_FILE", "data.json")                  # fallback only

# ── Binance Pay auto-deposit ──
BINANCE_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
BINANCE_HOST = os.getenv("BINANCE_HOST", "https://api.binance.com").rstrip("/")
BINANCE_ON = bool(BINANCE_KEY and BINANCE_SECRET)
STABLE = {"USDT", "USDC", "BUSD", "FDUSD"}          # currencies treated as 1 USD
MAX_PENDING = 3                                      # pending manual deposits per user

# ── Telegram account selling (Telethon) ──
try:
    TG_API_ID = int(os.getenv("TG_API_ID", "0").strip() or 0)
except ValueError:
    TG_API_ID = 0
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_CODE_LIMIT = 40                                   # max "Get Login Code" presses per account

# ── Anti-spam ──
SPAM_WINDOW = 6
SPAM_MAX = 8
SPAM_REPEAT = 4
SPAM_MUTE = 30

# Every button: key -> (label, default color, fallback emoji, group)
BTN = {
    # user - bottom menu
    "buy": ("Buy Product", "success", "🛒", "user"),
    "tgshop": ("Telegram Accounts", "success", "📱", "user"),
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
    "tg_code": ("Get Login Code", "success", "📩", "user"),
    "tg_done": ("Login Done", "primary", "🔓", "user"),
    # admin - bottom menu
    "a_dash": ("Dashboard", "primary", "📊", "admin"),
    "a_report": ("Sales Report", "primary", "📈", "admin"),
    "a_users": ("Users", "primary", "👥", "admin"),
    "a_prod": ("Products", "success", "🗃", "admin"),
    "a_tg": ("TG Accounts", "success", "📲", "admin"),
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
USER_KEYS = ["buy", "tgshop", "my_profile", "deposit", "history", "code", "support", "admin"]
ADMIN_KEYS = ["a_dash", "a_report", "a_users", "a_prod", "a_tg", "a_cat", "a_deps", "a_bc", "a_notice", "a_set",
              "a_subs", "a_style", "a_emoji", "a_exit"]

PERMS = {
    "dash": "Dashboard & Reports", "users": "Users", "prod": "Products & Stock", "tgacc": "TG Accounts",
    "cat": "Categories", "deps": "Deposits", "bc": "Broadcast", "notice": "Notice", "set": "Settings",
}
KEY_PERM = {
    "a_dash": "dash", "a_report": "dash", "a_users": "users", "a_prod": "prod", "a_tg": "tgacc", "a_cat": "cat",
    "a_deps": "deps", "a_bc": "bc", "a_notice": "notice", "a_set": "set",
    "a_subs": "owner", "a_style": "owner", "a_emoji": "owner", "a_exit": "",
}
OP_PERM = {
    "stats": "dash", "rep": "dash", "repcsv": "dash", "low": "dash",
    "cats": "cat", "addc": "cat", "c": "cat", "crn": "cat", "cdel": "cat", "cem": "cat",
    "pl": "prod", "addp": "prod", "addpc": "prod", "p": "prod", "tg": "prod", "del": "prod", "delok": "prod",
    "ep": "prod", "ed": "prod", "stk": "prod", "pem": "prod",
    "tga": "tgacc", "tgaadd": "tgacc", "tgv": "tgacc", "tgp": "tgacc", "tgd": "tgacc", "tgdok": "tgacc",
    "tgchk": "tgacc", "tgcode": "tgacc", "tgx": "tgacc",
    "deps": "deps", "d": "deps", "dok": "deps", "dno": "deps", "dh": "deps", "dss": "deps",
    "ul": "users", "find": "users", "u": "users", "ban": "users", "uo": "users", "o": "users",
    "adj": "users", "ut": "users",
    "bca": "bc", "bcgo": "bc",
    "set": "set", "tgl": "set", "sv": "set", "bint": "set",
    "sty": "owner", "stp": "owner", "sts": "owner", "emo": "owner", "emk": "owner",
    "subs": "owner", "sa": "owner", "saadd": "owner", "sap": "owner", "sadel": "owner", "sadelok": "owner",
}

EMOJI = {
    "fire": "🔥", "ok": "✅", "no": "❌", "wallet": "💳", "profile": "👤", "lock": "🔒",
    "notice": "📢", "stock": "🗃", "orders": "📦", "cart": "🛒", "bolt": "⚡", "party": "🎉",
    "stats": "📊", "users": "👥", "ban": "🚫", "settings": "⚙️", "invoice": "🧾",
    "money": "💵", "date": "📅", "crown": "👑", "broadcast": "📣", "star": "⭐", "down": "⬇️",
    "report": "📈", "low": "⚠️", "target": "🎯", "shield": "🛡", "hist": "📜", "photo": "🖼",
    "pending": "⏳", "auto": "🤖", "spam": "🚨", "credit": "🟢", "debit": "🔴", "import": "📥",
    "id": "🆔", "rank": "🏅", "name": "📛", "link": "🔗", "binance": "🟡",
    "bkash": "🌸", "nagad": "🟠", "manual": "👨‍💼",
    "phone": "📞", "globe": "🌍", "key": "🔐", "mail": "📩",
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
                                      "txns", "subs", "settings", "seq", "tgacc")}
SET = D["settings"]
store_lock = asyncio.Lock()
buy_lock = asyncio.Lock()     # ONE lock for every balance change (purchase, deposit, approve)
dep_lock = buy_lock
fb_ref = None
ADMIN_MODE: set[int] = set()
LOW_SENT: dict[int, int] = {}


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
    if uid in ADMIN_IDS:
        return True
    if not perm:
        return is_staff(uid)
    if perm == "owner":
        return False
    return perm in D["subs"].get(str(uid), {}).get("perms", [])


def staff_ids(perm=None) -> set:
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
    for p in D["products"].values():
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
    rows = [[RB("buy"), RB("tgshop")], [RB("my_profile"), RB("deposit")], [RB("history"), RB("code")], [RB("support")]]
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
    for aid in staff_ids(perm):
        try:
            if photo:
                await bot.send_photo(aid, photo, caption=text, reply_markup=markup)
            else:
                await bot.send_message(aid, text, reply_markup=markup)
        except Exception as e:
            log.warning("Cannot notify admin %s: %s", aid, e)


async def stamp(c: CallbackQuery, extra: str):
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


def pay_icon(name: str) -> str:
    n = name.lower()
    if "bkash" in n:
        return EMOJI["bkash"]
    if "nagad" in n:
        return EMOJI["nagad"]
    if "binance" in n:
        return EMOJI["binance"]
    return "💳"


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


TOGGLES = {"maintenance": "0", "antispam": "1", "auto_dep": "1"}


def flag(key: str) -> bool:
    return SET.get(key, TOGGLES.get(key, "0")) == "1"


def is_auto(method_name: str) -> bool:
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
         f"<i>Premium Proxy &amp; VPN · Telegram Accounts · Instant delivery · 24/7</i>\n\n"
         f"{E('profile')} <b>ID:</b> <code>{uid}</code>\n"
         f"{E('wallet')} <b>Balance:</b> {money(u['balance'])}\n")
    if SET.get("notice"):
        t += f"\n{E('notice')} <b>Notice</b>\n{SET['notice']}\n"
    return t + "\n<i>Use the menu below</i> 👇"


def desc_html(p) -> str:
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


# ── country flag from phone number ──
_DIAL = ("1:US:United States,7:RU:Russia,20:EG:Egypt,27:ZA:South Africa,30:GR:Greece,31:NL:Netherlands,"
         "32:BE:Belgium,33:FR:France,34:ES:Spain,36:HU:Hungary,39:IT:Italy,40:RO:Romania,41:CH:Switzerland,"
         "43:AT:Austria,44:GB:United Kingdom,45:DK:Denmark,46:SE:Sweden,47:NO:Norway,48:PL:Poland,49:DE:Germany,"
         "51:PE:Peru,52:MX:Mexico,53:CU:Cuba,54:AR:Argentina,55:BR:Brazil,56:CL:Chile,57:CO:Colombia,"
         "58:VE:Venezuela,60:MY:Malaysia,61:AU:Australia,62:ID:Indonesia,63:PH:Philippines,64:NZ:New Zealand,"
         "65:SG:Singapore,66:TH:Thailand,81:JP:Japan,82:KR:South Korea,84:VN:Vietnam,86:CN:China,90:TR:Turkey,"
         "91:IN:India,92:PK:Pakistan,93:AF:Afghanistan,94:LK:Sri Lanka,95:MM:Myanmar,98:IR:Iran,"
         "212:MA:Morocco,213:DZ:Algeria,216:TN:Tunisia,218:LY:Libya,220:GM:Gambia,221:SN:Senegal,"
         "233:GH:Ghana,234:NG:Nigeria,237:CM:Cameroon,249:SD:Sudan,251:ET:Ethiopia,254:KE:Kenya,"
         "255:TZ:Tanzania,256:UG:Uganda,260:ZM:Zambia,263:ZW:Zimbabwe,351:PT:Portugal,352:LU:Luxembourg,"
         "353:IE:Ireland,354:IS:Iceland,355:AL:Albania,358:FI:Finland,359:BG:Bulgaria,370:LT:Lithuania,"
         "371:LV:Latvia,372:EE:Estonia,373:MD:Moldova,374:AM:Armenia,375:BY:Belarus,380:UA:Ukraine,"
         "381:RS:Serbia,385:HR:Croatia,386:SI:Slovenia,420:CZ:Czechia,421:SK:Slovakia,852:HK:Hong Kong,"
         "853:MO:Macao,855:KH:Cambodia,856:LA:Laos,880:BD:Bangladesh,886:TW:Taiwan,960:MV:Maldives,"
         "961:LB:Lebanon,962:JO:Jordan,963:SY:Syria,964:IQ:Iraq,965:KW:Kuwait,966:SA:Saudi Arabia,"
         "967:YE:Yemen,968:OM:Oman,970:PS:Palestine,971:AE:UAE,972:IL:Israel,973:BH:Bahrain,974:QA:Qatar,"
         "975:BT:Bhutan,976:MN:Mongolia,977:NP:Nepal,992:TJ:Tajikistan,993:TM:Turkmenistan,994:AZ:Azerbaijan,"
         "995:GE:Georgia,996:KG:Kyrgyzstan,998:UZ:Uzbekistan")
DIAL: dict[str, tuple[str, str]] = {}
for _x in _DIAL.split(","):
    _c, _iso, _n = _x.split(":")
    DIAL[_c] = (_iso, _n)


def iso_flag(iso: str) -> str:
    iso = (iso or "").upper()
    if len(iso) == 2 and iso.isalpha():
        return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in iso)
    return "🌍"


def detect_country(phone: str) -> tuple[str, str]:
    d = re.sub(r"\D", "", phone)
    if d[:1] == "7" and d[1:2] in ("6", "7"):
        return "KZ", "Kazakhstan"
    for n in (3, 2, 1):
        if d[:n] in DIAL:
            return DIAL[d[:n]]
    return "", "Other"


def mask_phone(p: str) -> str:
    return p[:5] + "•" * max(1, len(p) - 7) + p[-2:]


# ── duplicate checks ──
def trx_key(t: str) -> str:
    return re.sub(r"\s+", "", t or "").upper()


def trx_used(trx: str):
    k = trx_key(trx)
    for d in D["deposits"].values():
        if d["status"] != "rejected" and trx_key(d.get("trx", "")) == k:
            return d
    return None


def ss_used(uniq: str) -> bool:
    return any(d.get("ss_uid") == uniq and d["status"] != "rejected" for d in D["deposits"].values())


def pending_count(uid: int) -> int:
    return sum(1 for d in D["deposits"].values() if d["user_id"] == uid and d["status"] == "pending")


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
async def _bn_get(session, start: int, end: int, limit: int = 100):
    qs = (f"startTimestamp={start}&endTimestamp={end}&limit={limit}"
          f"&recvWindow=30000&timestamp={int(time.time() * 1000)}")
    sig = hmac.new(BINANCE_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    async with session.get(f"{BINANCE_HOST}/sapi/v1/pay/transactions?{qs}&signature={sig}",
                           headers={"X-MBX-APIKEY": BINANCE_KEY}) as r:
        js = await r.json(content_type=None)
        return r.status, js


def _bn_ok(js) -> bool:
    return isinstance(js, dict) and (js.get("success") is True or str(js.get("code")) == "000000")


async def binance_find(order_id: str):
    """Returns ('ok', tx) | ('notfound', None) | ('error', None)."""
    import aiohttp
    oid = order_id.strip().upper()
    bounds = [0, 1, 3, 7, 15, 30]
    now = int(time.time() * 1000)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            for a, b in zip(bounds, bounds[1:]):
                _, js = await _bn_get(s, now - b * 86400000, now - a * 86400000)
                if not _bn_ok(js):
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


async def binance_test():
    """Returns (ok: bool, message: str) for the admin 'Test Binance API' button."""
    import aiohttp
    now = int(time.time() * 1000)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            status, js = await _bn_get(s, now - 86400000, now, limit=5)
    except Exception as e:
        return False, f"Connection failed: {e}"
    if _bn_ok(js):
        n = len(js.get("data") or [])
        return True, f"API works ✅ (HTTP {status}). Last 24h Pay transactions seen: {n}"
    return False, f"HTTP {status}\n{json.dumps(js, ensure_ascii=False)[:400]}"


# ═════════════ MIDDLEWARE ═════════════
async def refresh_menu(event, u, row):
    if not isinstance(event, Message):
        return
    mv = SET.get("mv", "0")
    if row.get("mv") != mv:
        row["mv"] = mv
        await put("users", u.id)
        if not (event.text or "").startswith("/start"):
            await event.answer("🔄 Menu updated", reply_markup=menu_kb(u.id))


class AntiSpam(BaseMiddleware):
    def __init__(self):
        self.hits: dict[int, deque] = {}
        self.mute: dict[int, float] = {}
        self.strikes: dict[int, tuple] = {}
        self.last: dict[int, tuple] = {}

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
        if len(self.hits) > 5000:
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
    btns.append([B("Telegram Accounts", "tgshop", "tgshop")])
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
    if o.get("tg_acc"):
        return tg_invoice(o, head)
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
    t = f"{E('money')} <b>Add Funds</b>\n{BAR}\n"
    for n, _ in methods:
        tag = f"{E('auto')} <i>Auto · instant</i>" if is_auto(n) else f"{E('manual')} <i>Admin verified</i>"
        t += f"{pay_icon(n)} <b>{esc(n)}</b> — {tag}\n"
    t += f"{BAR}\n{E('wallet')} <b>Your balance:</b> {money(get_user(ev.from_user.id)['balance'])}\n"
    t += f"<i>Minimum deposit: {money(min_deposit())}</i>\n\nChoose a payment method 👇"
    btns = [[B(n + (" · Auto ⚡" if is_auto(n) else ""), f"depm:{i}", "pay", fb=pay_icon(n))]
            for i, (n, _) in enumerate(methods)]
    btns.append([B("Cancel", "close", "cancel")])
    await show(ev, t, kb(*btns))


# ═════════════ MENU ROUTER ═════════════
menu = Router()
STAFF = F.from_user.id.func(is_staff)


@menu.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await tg_cleanup(m.from_user.id)
    ADMIN_MODE.discard(m.from_user.id)
    await m.answer(home_text(m.from_user.id), reply_markup=menu_kb(m.from_user.id))


@menu.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await tg_cleanup(m.from_user.id)
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
    elif k == "tgshop":
        await screen_tgshop(m)
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
    await tg_cleanup(m.from_user.id)
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
    elif k == "a_tg":
        await screen_tgadmin(m, 0)
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
    mk = tg_kb(o, back=True) if o.get("tg_acc") else kb([B("Back", "ords:0", "back")])
    await show(c, invoice_text(o), mk)


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
        await show(c, f"{pay_icon(name)} <b>{esc(name)}</b>  {E('auto')} <i>Auto verified</i>\n{BAR}\n"
                      f"{E('link')} <b>Pay ID / Account</b>\n<code>{esc(details)}</code>\n{BAR}\n"
                      f"1️⃣ Send <b>USDT</b> to the account above via Binance Pay\n"
                      f"2️⃣ Copy the <b>Order ID</b> of your payment\n"
                      f"3️⃣ Paste it here 👇\n{BAR}\n"
                      f"{E('money')} Rate: <b>1 USDT = {money(usd_rate())}</b>\n"
                      f"{E('bolt')} <i>Balance is added instantly after verification.</i>",
                   kb([B("Cancel", "close", "cancel")]))
        return
    await state.set_state(Dep.amount)
    await state.update_data(method=name)
    await show(c, f"{pay_icon(name)} <b>{esc(name)}</b>  {E('manual')} <i>Admin verified</i>\n{BAR}\n"
                  f"{E('link')} <b>Send money to</b>\n<code>{esc(details)}</code>\n{BAR}\n"
                  f"1️⃣ Send the money to the number above\n"
                  f"2️⃣ Type the <b>amount</b> you sent\n"
                  f"3️⃣ Send your <b>Transaction ID</b>\n"
                  f"4️⃣ Attach a screenshot <i>(optional)</i>\n{BAR}\n"
                  f"{E('pending')} <i>Approval usually takes a few minutes.</i>\n"
                  f"<i>Minimum: {money(min_deposit())}</i>\n\n"
                  f"✍️ <b>Now type the amount you sent:</b>",
               kb([B("Cancel", "close", "cancel")]))


@router.message(StateFilter(Dep.amount))
async def dep_amount(m: Message, state: FSMContext):
    try:
        amt = float((m.text or "").replace(",", "").strip())
    except ValueError:
        return await m.answer("❗ Please send a valid number, e.g. <code>500</code>")
    if amt < min_deposit():
        return await m.answer(f"❗ Minimum deposit is <b>{money(min_deposit())}</b>")
    await state.update_data(amount=amt)
    await state.set_state(Dep.trx)
    await m.answer(f"{E('ok')} Amount: <b>{money(amt)}</b>\n\n"
                   f"🧾 Now send your <b>Transaction ID</b> (TrxID):",
                   reply_markup=kb([B("Cancel", "close", "cancel")]))


async def auto_deposit(m: Message, state: FSMContext, d: dict, trx: str):
    wait = await m.answer(f"{E('pending')} Verifying your payment with Binance…")
    st, tx = await binance_find(trx)
    if st == "error":
        await state.clear()
        await notify_admins(m.bot, f"{E('low')} <b>Binance verification failed</b> (API error) for user "
                                   f"<code>{m.from_user.id}</code>, Order ID <code>{esc(trx)}</code>. "
                                   f"Open Settings → Test Binance API to see the exact error.", perm="set")
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
    await wait.edit_text(f"{E('ok')} <b>Deposit successful!</b>\n{BAR}\n"
                         f"{E('binance')} Received: <b>{amt:g} {esc(cur)}</b>\n"
                         f"{E('money')} Credited: <b>{money(credit)}</b>\n"
                         f"{E('wallet')} New balance: <b>{money(bal)}</b>",
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
    if pending_count(m.from_user.id) >= MAX_PENDING:
        await state.clear()
        return await m.answer(f"{E('pending')} You already have <b>{MAX_PENDING}</b> pending deposits.\n"
                              "Please wait until they are approved.")
    await state.update_data(trx=trx)
    await state.set_state(Dep.ss)
    await m.answer(f"{E('photo')} <b>Payment screenshot</b> <i>(optional)</i>\n\n"
                   "Send a screenshot of your payment to get approved faster, or tap <b>Skip</b>.\n"
                   "<i>Send it as a photo, not as a file.</i>",
                   reply_markup=kb([B("Skip & Submit", "depskip", "skip")],
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
    await chat_msg.answer(f"{E('ok')} <b>Deposit request submitted!</b>\n{BAR}\n"
                          f"{E('id')} Request: <b>#{did}</b>\n"
                          f"{pay_icon(d['method'])} Method: <b>{esc(d['method'])}</b>\n"
                          f"{E('money')} Amount: <b>{money(d['amount'])}</b>\n"
                          f"🧾 Trx: <code>{esc(d['trx'])}</code>\n{BAR}\n"
                          f"{E('pending')} <i>Our team will verify it shortly. You'll be notified once approved.</i>",
                          reply_markup=kb([B("My Transactions", "txh:0", "history")]))
    u = get_user(user.id) or {}
    prev = sum(x["amount"] for x in D["deposits"].values() if x["user_id"] == user.id and x["status"] == "approved")
    await notify_admins(
        bot, f"{E('money')} <b>New deposit #{did}</b>  {E('pending')}\n{BAR}\n"
             f"{E('profile')} {esc(u.get('full_name') or '-')} · @{esc(user.username or '-')}\n"
             f"{E('id')} <code>{user.id}</code> · {rank_of(u.get('spent', 0))}\n"
             f"{E('wallet')} Balance: {money(u.get('balance', 0))} · Past deposits: {money(prev)}\n{BAR}\n"
             f"{pay_icon(d['method'])} Method: <b>{esc(d['method'])}</b>\n"
             f"{E('money')} Amount: <b>{money(d['amount'])}</b>\n"
             f"🧾 Trx: <code>{esc(d['trx'])}</code>\n"
             f"{E('photo')} Screenshot: {'yes ⬆️' if ss else 'no'}",
        kb([B("Approve", f"adm:dok:{did}", "x_ok"), B("Reject", f"adm:dno:{did}", "x_no")],
           [B("User", f"adm:u:{user.id}", "x_edit", fb="👤")]),
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
class TgAdd(StatesGroup):
    phone = State(); code = State(); pwd = State(); setpwd = State(); price = State()
class TgPrice(StatesGroup):
    price = State()


# ── Dashboard ──
async def screen_stats(ev):
    day = int(time.time()) - 86400
    users, orders, deps = D["users"].values(), D["orders"].values(), D["deposits"].values()
    tg_left = sum(1 for a in D["tgacc"].values() if not a["sold"] and a.get("active", 1))
    t = (f"{E('stats')} <b>Dashboard</b>\n{BAR}\n"
         f"{E('users')} Users: <b>{len(D['users'])}</b> (+{sum(1 for u in users if u['joined_at'] > day)} today)\n"
         f"{E('stock')} Products: <b>{len(D['products'])}</b> · Stock left: "
         f"<b>{sum(1 for s in D['stock'].values() if not s['sold'])}</b>\n"
         f"{E('phone')} TG accounts in stock: <b>{tg_left}</b>\n"
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
    await show(ev, "📁 <b>Categories</b>\n\nTap a category to edit it.", kb(*btns))


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
    await show(ev, f"{E('money')} <b>Pending Deposits</b>  <i>({len(rows)})</i>", kb(*btns))


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
    t = (f"{E('money')} <b>Deposit #{d['id']}</b>\n{BAR}\nUser: <code>{d['user_id']}</code>\n"
         f"Method: {pay_icon(d['method'])} {esc(d['method'])}\n"
         f"Amount: <b>{money(d['amount'])}</b>" + (f" ({esc(d['raw'])})" if d.get("raw") else "") + "\n"
         f"Trx: <code>{esc(d['trx'])}</code>\nStatus: <b>{d['status']}</b>"
         + (" · auto" if d.get("auto") else "") + f"\nDate: {fdate(d['created_at'])}")
    rows = []
    if d["status"] == "pending":
        rows.append([B("Approve", f"adm:dok:{d['id']}", "x_ok"), B("Reject", f"adm:dno:{d['id']}", "x_no")])
    if d.get("ss"):
        rows.append([B("View Screenshot", f"adm:dss:{d['id']}", "x_shot")])
    rows.append([B("User", f"adm:u:{d['user_id']}", "x_edit", fb="👤")])
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
            d["user_id"], f"{E('ok')} <b>Deposit Approved!</b>\n{BAR}\n"
                          f"{E('money')} Amount: <b>{money(d['amount'])}</b>\n"
                          f"{E('wallet')} New balance: <b>{money(bal if bal is not None else 0)}</b>",
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


# ── Users ──
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
    async with buy_lock:
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
                "<code>bKash|017XXXXXXXX (Send Money)\nNagad|017XXXXXXXX (Send Money)\nBinance Pay|XXXXXXXX</code>\n"
                "<i>Any method whose name contains \"binance\" is auto-verified (when API keys are set). "
                "bKash / Nagad / others → admin approval.</i>",
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
        [B("Test Binance API", "adm:bint", "x_refresh", fb="🟡")],
        [B(f"Auto Deposit: {onoff('auto_dep')}", "adm:tgl:auto_dep", None, fb="", st="success" if flag("auto_dep") else "danger"),
         B(f"Anti-Spam: {onoff('antispam')}", "adm:tgl:antispam", None, fb="", st="success" if flag("antispam") else "danger")],
        [B("Maintenance: ON" if flag("maintenance") else "Maintenance: OFF", "adm:tgl:maintenance", None, fb="🛠",
           st="danger" if flag("maintenance") else None)]))


@admin.callback_query(F.data == "adm:bint")
async def adm_bint(c: CallbackQuery):
    if not BINANCE_ON:
        return await c.answer("BINANCE_API_KEY / SECRET not set.", show_alert=True)
    await c.answer("Testing…")
    ok, msg = await binance_test()
    await c.message.answer(f"{E('binance')} <b>Binance API test</b>\n{BAR}\n"
                           f"{E('ok') if ok else E('no')} <code>{esc(msg)}</code>\n\n"
                           + ("" if ok else "<i>Check: key has “Enable Reading”, IP restriction (use Unrestricted or "
                                            "your server IP), and the server region is not blocked by Binance "
                                            "(try BINANCE_HOST=https://api1.binance.com).</i>"))


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
    if perm in perms:
        perms.remove(perm)
    else:
        perms.append(perm)
    s["perms"] = perms
    await put("subs", uid)
    t, mk = sub_panel(uid)
    await show(c, t, mk)


@admin.callback_query(F.data.startswith("adm:sadel:"))
async def adm_sadel(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    await show(c, "⚠️ <b>Remove this sub admin?</b>",
               kb([B("Yes, remove", f"adm:sadelok:{uid}", "x_no", fb="🗑"), B("No", f"adm:sa:{uid}", "x_ok")]))


@admin.callback_query(F.data.startswith("adm:sadelok:"))
async def adm_sadelok(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    D["subs"].pop(str(uid), None)
    await put("subs", uid)
    ADMIN_MODE.discard(uid)
    await c.answer("Removed ✅")
    await screen_subs(c)


# ── Button colors (owner only) ──
async def screen_styles(ev, page=0):
    keys = list(BTN.keys())
    items, page, pages = paginate(keys, page, 10)
    btns = [[B(f"{STYLE_MARK[bstyle(k)]} {BTN[k][0]}", f"adm:stp:{k}", None, fb="")] for k in items]
    nr = nav_row("adm:sty", page, pages)
    if nr:
        btns.append(nr)
    await show(ev, f"🎨 <b>Button Colors</b>\n\nTap a button to change its color.\n"
                   f"<i>🔵 Blue · 🟢 Green · 🔴 Red · ⚪ Default</i>", kb(*btns))


@admin.callback_query(F.data.startswith("adm:sty:"))
async def adm_sty(c: CallbackQuery):
    await screen_styles(c, int(c.data.split(":")[2]))


async def screen_style_pick(c: CallbackQuery, key: str):
    cur = bstyle(key)
    t = (f"🎨 <b>{esc(BTN[key][0])}</b>\nCurrent: <b>{STYLE_LABEL.get(cur or 'none')}</b>\n\nChoose a color:")
    rows = [[B(STYLE_LABEL[s], f"adm:sts:{key}:{s}", None, fb="") for s in ("primary", "success")],
            [B(STYLE_LABEL[s], f"adm:sts:{key}:{s}", None, fb="") for s in ("danger", "none")],
            [B("Reset to default", f"adm:sts:{key}:reset", "x_refresh")],
            [B("Back", "adm:sty:0", "x_nav")]]
    await show(c, t, kb(*rows))


@admin.callback_query(F.data.startswith("adm:stp:"))
async def adm_stp(c: CallbackQuery):
    key = c.data.split(":")[2]
    if key not in BTN:
        return await c.answer("Unknown", show_alert=True)
    await screen_style_pick(c, key)


@admin.callback_query(F.data.startswith("adm:sts:"))
async def adm_sts(c: CallbackQuery):
    _, _, key, style = c.data.split(":")
    if key not in BTN:
        return await c.answer("Unknown", show_alert=True)
    if style == "reset":
        await del_setting(f"style:{key}")
    elif style in STYLE_LABEL:
        await set_setting(f"style:{key}", style)
    await bump_menu()
    await screen_style_pick(c, key)


# ── Custom emoji (owner only) ──
def emoji_keys() -> list[str]:
    return list(BTN.keys()) + [k for k in EMOJI if k not in BTN]


async def screen_emojis(ev, page=0):
    items, page, pages = paginate(emoji_keys(), page, 12)
    btns, row = [], []
    for k in items:
        row.append(B(f"{'⭐' if emoji_id(k) else '▫️'} {BTN[k][0] if k in BTN else k}", f"adm:emk:{k}", None, fb=""))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    nr = nav_row("adm:emo", page, pages)
    if nr:
        btns.append(nr)
    await show(ev, "⭐ <b>Custom Emoji</b>\n\nTap an item, then send ONE custom (premium) emoji.\n"
                   "<i>⭐ = custom emoji is set</i>", kb(*btns))


@admin.callback_query(F.data.startswith("adm:emo:"))
async def adm_emo(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await screen_emojis(c, int(c.data.split(":")[2]))


def extract_emoji_id(m: Message):
    for e in (m.entities or []):
        if e.type == "custom_emoji":
            return e.custom_emoji_id
    t = (m.text or "").strip()
    return t if t.isdigit() and len(t) > 10 else None


async def ask_emoji(c: CallbackQuery, state: FSMContext, back: str, **data):
    await state.set_state(EmojiS.value)
    await state.update_data(back=back, **data)
    await show(c, "⭐ Send <b>ONE custom emoji</b> (or its numeric emoji ID).\nSend <code>clear</code> to remove it.",
               kb([B("Cancel", back, "x_no")]))


@admin.callback_query(F.data.startswith("adm:emk:"))
async def adm_emk(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    if key not in emoji_keys():
        return await c.answer("Unknown", show_alert=True)
    await ask_emoji(c, state, "adm:emo:0", target="key", key=key)


@admin.callback_query(F.data.startswith("adm:cem:"))
async def adm_cem(c: CallbackQuery, state: FSMContext):
    cid = c.data.split(":")[2]
    await ask_emoji(c, state, f"adm:c:{cid}", target="cat", id=cid)


@admin.callback_query(F.data.startswith("adm:pem:"))
async def adm_pem(c: CallbackQuery, state: FSMContext):
    pid = c.data.split(":")[2]
    await ask_emoji(c, state, f"adm:p:{pid}", target="prod", id=pid)


@admin.message(StateFilter(EmojiS.value))
async def emoji_save(m: Message, state: FSMContext):
    d = await state.get_data()
    clear = (m.text or "").strip().lower() == "clear"
    eid = "" if clear else extract_emoji_id(m)
    if eid is None:
        return await m.answer("❗ Send a <b>custom emoji</b> (premium sticker-emoji) or its numeric ID.")
    tgt = d.get("target")
    if tgt == "key":
        if eid:
            await set_setting(f"emoji:{d['key']}", eid)
        else:
            await del_setting(f"emoji:{d['key']}")
        await bump_menu()
    elif tgt == "cat" and D["cats"].get(str(d["id"])):
        D["cats"][str(d["id"])]["emoji"] = eid
        await put("cats", d["id"])
    elif tgt == "prod" and prod(d["id"]):
        prod(d["id"])["emoji"] = eid
        await put("products", d["id"])
    await state.clear()
    await m.answer(f"{E('ok')} Emoji {'removed' if not eid else 'saved'}.",
                   reply_markup=kb([B("Back", d.get("back", "adm:emo:0"), "x_nav")]))


# ═════════════════════════════════════════════════════════════════
#                     TELEGRAM ACCOUNT SELLING
# ═════════════════════════════════════════════════════════════════
try:
    from telethon import TelegramClient, errors as tgerr
    from telethon.sessions import StringSession
    from telethon.tl.functions.account import GetPasswordRequest
except ImportError:                      # telethon not installed -> feature disabled, bot still runs
    TelegramClient = None
    tgerr = None

TG_ON = bool(TG_API_ID and TG_API_HASH and TelegramClient)
TG_PENDING: dict[int, dict] = {}         # admin_id -> login in progress {"client","phone","hash",...}
TG_LOCKS: dict[int, asyncio.Lock] = {}   # one lock per stored account (never use a session twice at once)
TG_COOL: dict[int, float] = {}           # user_id -> last "Get Login Code" time
CODE_RE = re.compile(r"(?:code|কোড)\D{0,12}(\d{5,6})", re.I)


def tg_client(session: str = ""):
    return TelegramClient(StringSession(session), TG_API_ID, TG_API_HASH,
                          device_model="Samsung SM-S918B", system_version="Android 14",
                          app_version="10.14.5", lang_code="en", system_lang_code="en-US")


async def tg_cleanup(uid: int):
    """Disconnect a half-finished admin login (session string is NOT revoked)."""
    p = TG_PENDING.pop(uid, None)
    if p:
        try:
            await p["client"].disconnect()
        except Exception:
            pass


def extract_code(text: str):
    m = CODE_RE.search(text or "") or re.search(r"\b(\d{5,6})\b", text or "")
    return m.group(1) if m else None


def _dead_errors():
    return (tgerr.AuthKeyUnregisteredError, tgerr.UserDeactivatedError, tgerr.UserDeactivatedBanError,
            tgerr.SessionRevokedError, tgerr.SessionExpiredError)


async def tg_latest_code(acc: dict, since: float):
    """Read the newest login code from the 'Telegram' service chat (777000) of the stored session.
    Returns ('ok',(code,ts)) | ('none',None) | ('dead',None) | ('error',None)."""
    lock = TG_LOCKS.setdefault(acc["id"], asyncio.Lock())
    async with lock:
        cl = tg_client(acc.get("session", ""))
        try:
            await cl.connect()
            if not await cl.is_user_authorized():
                return "dead", None
            async for msg in cl.iter_messages(777000, limit=10):
                ts = msg.date.timestamp()
                if ts < since:
                    break
                code = extract_code(msg.raw_text or "")
                if code:
                    return "ok", (code, int(ts))
            return "none", None
        except _dead_errors():
            return "dead", None
        except Exception as e:
            log.warning("tg_latest_code failed for #%s: %s", acc.get("id"), e)
            return "error", None
        finally:
            try:
                await cl.disconnect()
            except Exception:
                pass


async def tg_check(acc: dict):
    """Returns (ok, text) - is the stored session still logged in?"""
    lock = TG_LOCKS.setdefault(acc["id"], asyncio.Lock())
    async with lock:
        cl = tg_client(acc.get("session", ""))
        try:
            await cl.connect()
            if not await cl.is_user_authorized():
                return False, "Session is NOT logged in (revoked / expired)."
            me = await cl.get_me()
            return True, f"Session OK ✅ {me.first_name or ''} (id {me.id})"
        except _dead_errors():
            return False, "Session is dead (revoked / account banned)."
        except Exception as e:
            return False, f"Check failed: {e}"[:190]
        finally:
            try:
                await cl.disconnect()
            except Exception:
                pass


async def tg_logout(acc: dict) -> bool:
    """Terminate ONLY the bot's session on that account. True = done (or already dead)."""
    lock = TG_LOCKS.setdefault(acc["id"], asyncio.Lock())
    async with lock:
        cl = tg_client(acc.get("session", ""))
        try:
            await cl.connect()
            if await cl.is_user_authorized():
                await cl.log_out()
            return True
        except _dead_errors():
            return True
        except Exception as e:
            log.warning("tg_logout failed for #%s: %s", acc.get("id"), e)
            return False
        finally:
            try:
                await cl.disconnect()
            except Exception:
                pass


# ── user side ──
def tg_avail() -> list[dict]:
    return [a for a in D["tgacc"].values() if not a["sold"] and a.get("active", 1) and a.get("session")]


def tg_invoice(o: dict, head=None) -> str:
    a = D["tgacc"].get(str(o.get("tg_acc"))) or {}
    pw = a.get("twofa") or ""
    live = bool(a.get("session"))
    t = f"{head}\n\n" if head else ""
    t += (f"{E('invoice')} <b>ORDER #{o['id']}</b>\n{BAR}\n"
          f"{E('globe')} <b>Country:</b> {a.get('flag', '🌍')} {esc(a.get('cname', '-'))}\n"
          f"{E('date')} <b>Date:</b> {fdate(o['created_at'])}\n"
          f"{E('money')} <b>Paid:</b> {money(o['price'])}\n{BAR}\n"
          f"┌── {E('phone')} <b>NUMBER</b>\n└ <code>{esc(a.get('phone', '-'))}</code>\n\n"
          f"┌── {E('key')} <b>TWO-STEP password</b>\n└ <code>{esc(pw) if pw else 'No password'}</code>\n{BAR}\n")
    if live:
        t += ("<b>How to login</b>\n"
              "1️⃣ Telegram → <b>Add Account</b> → enter the number above\n"
              "2️⃣ Tap <b>Get Login Code</b> below — the code appears here\n"
              "3️⃣ Enter the code in Telegram, then the TWO-STEP password if asked\n"
              "4️⃣ When you are logged in, tap <b>Login Done</b>\n")
    else:
        t += f"{E('ok')} <i>Bot session removed — this account is now fully yours.</i>\n"
    return t + f"{BAR}\n<i>Thank you for shopping with {esc(BRAND)}!</i>"


def tg_kb(o: dict, back: bool = False):
    a = D["tgacc"].get(str(o.get("tg_acc"))) or {}
    rows = []
    if a.get("session"):
        rows.append([B("Get Login Code", f"tgcode:{o['id']}", "tg_code")])
        rows.append([B("Login Done", f"tgdone:{o['id']}", "tg_done")])
    rows.append([B("My Orders", "ords:0", "my_orders")] if not back else [B("Back", "ords:0", "back")])
    return kb(*rows)


async def screen_tgshop(ev):
    groups: dict[tuple, list] = {}
    for a in tg_avail():
        groups.setdefault((a.get("iso") or "XX", a.get("cname") or "Other"), []).append(a)
    t = (f"{E('phone')} <b>Telegram Accounts</b>\n"
         f"<i>Two-step password included · login code delivered right here</i>\n{BAR}\n")
    btns = []
    if not groups:
        t += "<i>No accounts in stock right now. Please check back soon.</i>"
    else:
        t += "Choose a country 👇"
        for (iso, cn), lst in sorted(groups.items(), key=lambda x: x[0][1]):
            low = min(a["price"] for a in lst)
            btns.append([B(f"{cn} · {len(lst)} left · from {money(low)}", f"tgc:{iso}:0", "item",
                           fb=iso_flag("" if iso == "XX" else iso))])
    btns.append([B("Cancel", "close", "cancel")])
    await show(ev, t, kb(*btns))


async def screen_tgcountry(ev, iso: str, page: int = 0):
    lst = sorted((a for a in tg_avail() if (a.get("iso") or "XX") == iso), key=lambda a: (a["price"], a["id"]))
    if not lst:
        return await screen_tgshop(ev)
    items, page, pages = paginate(lst, page, 8)
    fl, cn = lst[0]["flag"], lst[0]["cname"]
    t = (f"{fl} <b>{esc(cn)}</b> accounts  <i>({len(lst)} in stock)</i>\n{BAR}\nPick a number 👇")
    btns = [[B(f"{mask_phone(a['phone'])} · {money(a['price'])}", f"tgb:{a['id']}", "item", fb=a["flag"])] for a in items]
    nr = nav_row(f"tgc:{iso}", page, pages)
    if nr:
        btns.append(nr)
    btns.append([B("Back", "tgshop", "back")])
    await show(ev, t, kb(*btns))


@router.callback_query(F.data == "tgshop")
async def cb_tgshop(c: CallbackQuery):
    await screen_tgshop(c)


@router.callback_query(F.data.startswith("tgc:"))
async def cb_tgc(c: CallbackQuery):
    parts = c.data.split(":")
    await screen_tgcountry(c, parts[1], int(parts[2]) if len(parts) > 2 else 0)


@router.callback_query(F.data.startswith("tgb:"))
async def cb_tgb(c: CallbackQuery):
    a = D["tgacc"].get(c.data.split(":")[1])
    u = get_user(c.from_user.id)
    if not a or a["sold"] or not a.get("active", 1):
        return await c.answer("This account is no longer available.", show_alert=True)
    back = B("Back", f"tgc:{a.get('iso') or 'XX'}:0", "back")
    if u["balance"] < a["price"]:
        t = (f"{E('no')} <b>Insufficient balance</b>\n\nPrice: {money(a['price'])}\n"
             f"Your balance: {money(u['balance'])}\n\nPlease add funds first.")
        return await show(c, t, kb([B("Add Funds", "dep", "deposit")], [back]))
    t = (f"{E('cart')} <b>Confirm purchase</b>\n{BAR}\n"
         f"{E('globe')} Country: <b>{a['flag']} {esc(a['cname'])}</b>\n"
         f"{E('phone')} Number: <code>{mask_phone(a['phone'])}</code>\n"
         f"{E('key')} Two-step password: <b>included</b>\n"
         f"{E('money')} Price: <b>{money(a['price'])}</b>\n"
         f"{E('wallet')} Balance after: <b>{money(u['balance'] - a['price'])}</b>\n{BAR}\n"
         f"<i>The full number + password are shown right after payment.</i>")
    await show(c, t, kb([B("Confirm", f"tgok:{a['id']}", "confirm"), B("Cancel", f"tgc:{a.get('iso') or 'XX'}:0", "cancel")]))


@router.callback_query(F.data.startswith("tgok:"))
async def cb_tgok(c: CallbackQuery):
    aid, uid = c.data.split(":")[1], c.from_user.id
    async with buy_lock:
        a, u = D["tgacc"].get(aid), get_user(uid)
        if not a or a["sold"] or not a.get("active", 1):
            return await c.answer("Sorry, this account was just sold.", show_alert=True)
        if u["balance"] < a["price"]:
            return await c.answer("Insufficient balance.", show_alert=True)
        now = int(time.time())
        a["sold"], a["buyer"], a["sold_at"] = 1, uid, now
        u["balance"] -= a["price"]
        u["spent"] += a["price"]
        oid = await nid("orders")
        a["order_id"] = oid
        o = {"id": oid, "user_id": uid, "product_id": 0, "product_name": f"{a['flag']} Telegram Account",
             "price": a["price"], "data": f"Number: {a['phone']}\n2FA Password: {a.get('twofa') or 'none'}",
             "balance_after": u["balance"], "created_at": now, "tg_acc": int(aid)}
        D["orders"][str(oid)] = o
        await put("tgacc", aid)
        await put("users", uid)
        await put("orders", oid)
        await log_txn(uid, "purchase", -a["price"], f"#{oid} TG {a['flag']} {mask_phone(a['phone'])}", bal=u["balance"])
    head = f"{E('party')} <b>Telegram account purchase successful, Now enjoy!</b> {E('ok')}"
    await show(c, tg_invoice(o, head), tg_kb(o))
    await notify_admins(c.bot, f"{E('phone')} <b>TG account sold · order #{oid}</b>\n"
                               f"User: <code>{uid}</code> (@{esc(c.from_user.username or '-')})\n"
                               f"{a['flag']} {esc(a['cname'])} · {esc(a['phone'])} · {money(a['price'])}\n"
                               f"In stock now: <b>{len(tg_avail())}</b>", perm="tgacc")


def _tg_order(c: CallbackQuery):
    o = D["orders"].get(c.data.split(":")[1])
    if not o or o["user_id"] != c.from_user.id or not o.get("tg_acc"):
        return None, None
    return o, D["tgacc"].get(str(o["tg_acc"]))


@router.callback_query(F.data.startswith("tgcode:"))
async def cb_tgcode(c: CallbackQuery):
    o, a = _tg_order(c)
    if not o or not a:
        return await c.answer("Order not found.", show_alert=True)
    if not a.get("session"):
        return await c.answer("Bot session was already removed.", show_alert=True)
    now = time.monotonic()
    if now - TG_COOL.get(c.from_user.id, 0) < 6:
        return await c.answer("⏳ Wait a few seconds and try again.")
    TG_COOL[c.from_user.id] = now
    if int(a.get("code_n", 0)) >= TG_CODE_LIMIT:
        return await c.answer("Code request limit reached. Please contact support.", show_alert=True)
    await c.answer("🔎 Checking for your code…")
    st, res = await tg_latest_code(a, o["created_at"] - 30)
    stamp_t = f"\n<i>Checked {datetime.now().strftime('%H:%M:%S')}</i>"
    if st == "ok":
        code, ts = res
        a["code_n"] = int(a.get("code_n", 0)) + 1
        await put("tgacc", a["id"])
        extra = (f"\n\n{E('mail')} <b>LOGIN CODE:</b> <code>{code}</code>\n"
                 f"<i>Received {fdate(ts)} — enter it in Telegram now.</i>")
    elif st == "none":
        extra = ("\n\n⏳ <b>No code yet.</b>\nStart the login in Telegram first (enter the number and tap Next), "
                 "then press <b>Get Login Code</b> again.")
    elif st == "dead":
        extra = "\n\n❌ <b>This session is no longer active.</b> Please contact support."
        await notify_admins(c.bot, f"{E('low')} <b>TG session dead</b> — account <code>{esc(a['phone'])}</code> "
                                   f"(order #{o['id']}, user <code>{o['user_id']}</code>)", perm="tgacc")
    else:
        extra = "\n\n⚠️ Temporary error while reading the code. Please try again in a moment."
    await show(c, tg_invoice(o) + extra + stamp_t, tg_kb(o))


@router.callback_query(F.data.startswith("tgdone:"))
async def cb_tgdone(c: CallbackQuery):
    o, a = _tg_order(c)
    if not o or not a:
        return await c.answer("Order not found.", show_alert=True)
    if not a.get("session"):
        return await c.answer("Already done ✅", show_alert=True)
    await show(c, f"🔓 <b>Confirm: Login Done?</b>\n\nThe bot will remove its own session from this account.\n"
                  f"{E('low')} After this you <b>cannot</b> get login codes here anymore, so only confirm when "
                  f"you are logged in on your device.",
               kb([B("Yes, I'm logged in", f"tgdoneok:{o['id']}", "confirm"), B("No", f"o:{o['id']}", "cancel")]))


@router.callback_query(F.data.startswith("tgdoneok:"))
async def cb_tgdoneok(c: CallbackQuery):
    o, a = _tg_order(c)
    if not o or not a:
        return await c.answer("Order not found.", show_alert=True)
    if a.get("session"):
        await c.answer("Removing bot session…")
        if not await tg_logout(a):
            return await show(c, tg_invoice(o) + f"\n\n{E('no')} Could not remove the session now. Try again shortly.",
                              tg_kb(o))
        a["session"], a["released"] = "", int(time.time())
        await put("tgacc", a["id"])
        await notify_admins(c.bot, f"{E('ok')} Order #{o['id']}: buyer confirmed login, bot session removed "
                                   f"({esc(a['phone'])}).", perm="tgacc")
    await show(c, tg_invoice(o), tg_kb(o))


# ── admin side ──
async def screen_tgadmin(ev, page: int = 0):
    rows = sorted(D["tgacc"].values(), key=lambda a: -a["id"])
    items, page, pages = paginate(rows, page, 8)
    sold = sum(1 for a in rows if a["sold"])
    t = (f"{E('phone')} <b>Telegram Accounts</b>\n{BAR}\n"
         f"In stock: <b>{len(tg_avail())}</b> · Sold: <b>{sold}</b> · Total: <b>{len(rows)}</b>")
    if not TG_ON:
        t += ("\n\n⚠️ <b>Adding is disabled.</b> Set <code>TG_API_ID</code> + <code>TG_API_HASH</code> "
              "(my.telegram.org) and install <code>telethon</code>.")
    btns = [[B(f"{'🔴' if a['sold'] else '🟢'} {a['flag']} {a['phone']} · {money(a['price'])}",
               f"adm:tgv:{a['id']}", "x_item", fb="")] for a in items]
    nr = nav_row("adm:tga", page, pages)
    if nr:
        btns.append(nr)
    btns.append([B("Add Account", "adm:tgaadd", "x_add")])
    await show(ev, t, kb(*btns))


@admin.callback_query(F.data.startswith("adm:tga:"))
async def adm_tga(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await tg_cleanup(c.from_user.id)
    await screen_tgadmin(c, int(c.data.split(":")[2]))


@admin.callback_query(F.data == "adm:tgx")
async def adm_tgx(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await tg_cleanup(c.from_user.id)
    await screen_tgadmin(c, 0)


async def render_tg_account(c: CallbackQuery, aid: str):
    a = D["tgacc"].get(aid)
    if not a:
        return await c.answer("Not found", show_alert=True)
    if a["sold"]:
        b = get_user(a.get("buyer")) or {}
        status = f"🔴 Sold to <code>{a.get('buyer')}</code> ({esc(b.get('full_name') or '-')}) · order #{a.get('order_id')}"
    else:
        status = "🟢 In stock" if a.get("active", 1) else "⚪ Hidden"
    t = (f"{E('phone')} <b>#{a['id']} {a['flag']} {esc(a['cname'])}</b>\n{BAR}\n"
         f"{E('phone')} Number: <code>{esc(a['phone'])}</code>\n"
         f"{E('key')} 2FA: <code>{esc(a.get('twofa') or 'none')}</code>\n"
         f"{E('money')} Price: <b>{money(a['price'])}</b>\n"
         f"Status: {status}\n"
         f"Bot session: <b>{'logged in ✅' if a.get('session') else 'removed'}</b>\n"
         f"Code requests: <b>{int(a.get('code_n', 0))}</b>\n"
         f"Added: {fdate(a.get('added_at', 0))}")
    rows = []
    if a.get("session"):
        rows.append([B("Check Session", f"adm:tgchk:{aid}", "x_refresh"), B("Latest Code", f"adm:tgcode:{aid}", "x_edit", fb="📩")])
    rows.append([B("Edit Price", f"adm:tgp:{aid}", "x_edit"), B("Delete", f"adm:tgd:{aid}", "x_no", fb="🗑")])
    rows.append([B("Back", "adm:tga:0", "x_nav")])
    await show(c, t, kb(*rows))


@admin.callback_query(F.data.startswith("adm:tgv:"))
async def adm_tgv(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_tg_account(c, c.data.split(":")[2])


@admin.callback_query(F.data.startswith("adm:tgchk:"))
async def adm_tgchk(c: CallbackQuery):
    a = D["tgacc"].get(c.data.split(":")[2])
    if not a or not a.get("session"):
        return await c.answer("No active session.", show_alert=True)
    await c.answer("Checking…")
    ok, msg = await tg_check(a)
    await c.message.answer(f"{E('ok') if ok else E('no')} <code>{esc(a['phone'])}</code> — {esc(msg)}")


@admin.callback_query(F.data.startswith("adm:tgcode:"))
async def adm_tgcode(c: CallbackQuery):
    a = D["tgacc"].get(c.data.split(":")[2])
    if not a or not a.get("session"):
        return await c.answer("No active session.", show_alert=True)
    await c.answer("Reading…")
    st, res = await tg_latest_code(a, time.time() - 3600)
    if st == "ok":
        txt = f"{E('mail')} <code>{esc(a['phone'])}</code> latest code (last 1h): <code>{res[0]}</code> · {fdate(res[1])}"
    elif st == "none":
        txt = f"No login code in the last hour for <code>{esc(a['phone'])}</code>."
    else:
        txt = f"{E('no')} Could not read ({st}) for <code>{esc(a['phone'])}</code>."
    await c.message.answer(txt)


@admin.callback_query(F.data.startswith("adm:tgp:"))
async def adm_tgp(c: CallbackQuery, state: FSMContext):
    await state.set_state(TgPrice.price)
    await state.update_data(aid=c.data.split(":")[2])
    await show(c, "✏️ Send the <b>new price</b>:", kb([B("Cancel", "adm:tga:0", "x_no")]))


@admin.message(StateFilter(TgPrice.price))
async def tgp_save(m: Message, state: FSMContext):
    try:
        price = float((m.text or "").replace(",", "").strip())
        if price < 0:
            raise ValueError
    except ValueError:
        return await m.answer("❗ Send a valid number.")
    d = await state.get_data()
    a = D["tgacc"].get(d["aid"])
    if a:
        a["price"] = price
        await put("tgacc", d["aid"])
    await state.clear()
    await m.answer(f"{E('ok')} Price updated to <b>{money(price)}</b>",
                   reply_markup=kb([B("Back to account", f"adm:tgv:{d['aid']}", "x_nav")]))


@admin.callback_query(F.data.startswith("adm:tgd:"))
async def adm_tgd(c: CallbackQuery):
    aid = c.data.split(":")[2]
    a = D["tgacc"].get(aid)
    if not a:
        return await c.answer("Not found", show_alert=True)
    note = ("The bot session will also be logged out from this account." if (a.get("session") and not a["sold"])
            else "Only the record is removed.")
    await show(c, f"⚠️ <b>Delete {a['flag']} {esc(a['phone'])}?</b>\n<i>{note}</i>",
               kb([B("Yes, delete", f"adm:tgdok:{aid}", "x_no", fb="🗑"), B("No", f"adm:tgv:{aid}", "x_ok")]))


@admin.callback_query(F.data.startswith("adm:tgdok:"))
async def adm_tgdok(c: CallbackQuery):
    aid = c.data.split(":")[2]
    a = D["tgacc"].get(aid)
    if a and a.get("session") and not a["sold"]:
        await c.answer("Deleting…")
        await tg_logout(a)
    D["tgacc"].pop(aid, None)
    await put("tgacc", aid)
    await screen_tgadmin(c, 0)


# ── add account: phone -> code -> (2FA) -> price ──
CANCEL_TG = lambda: kb([B("Cancel", "adm:tgx", "x_no")]) # noqa: E731


@admin.callback_query(F.data == "adm:tgaadd")
async def adm_tgaadd(c: CallbackQuery, state: FSMContext):
    if not TG_ON:
        return await c.answer("Set TG_API_ID / TG_API_HASH and install telethon first.", show_alert=True)
    await tg_cleanup(c.from_user.id)
    await state.clear()
    await state.set_state(TgAdd.phone)
    await show(c, f"{E('phone')} <b>Add Telegram Account</b>\n{BAR}\n"
                  "Send the <b>phone number</b> with country code:\n<code>+8801XXXXXXXXX</code>\n\n"
                  "<i>The bot will log in to this account and keep the session, so the buyer's login "
                  "code can be shown inside the bot.</i>", CANCEL_TG())


@admin.message(StateFilter(TgAdd.phone))
async def tg_phone(m: Message, state: FSMContext):
    digits = re.sub(r"\D", "", m.text or "")
    if not 7 <= len(digits) <= 15:
        return await m.answer("❗ Send a valid number with country code, e.g. <code>+8801712345678</code>")
    phone = "+" + digits
    if any(a["phone"] == phone for a in D["tgacc"].values()):
        return await m.answer("❗ This number is already in the list.")
    iso, cname = detect_country(phone)
    wait = await m.answer(f"{E('pending')} Sending login code to <code>{phone}</code>…")
    cl = tg_client()
    try:
        await cl.connect()
        sent = await cl.send_code_request(phone)
    except tgerr.PhoneNumberInvalidError:
        await cl.disconnect()
        return await wait.edit_text("❌ Telegram says this phone number is invalid. Send another number.")
    except tgerr.PhoneNumberBannedError:
        await cl.disconnect()
        return await wait.edit_text("❌ This number is banned by Telegram.")
    except tgerr.FloodWaitError as e:
        await cl.disconnect()
        return await wait.edit_text(f"⏳ Telegram flood-wait: try again after <b>{e.seconds}s</b>.")
    except Exception as e:
        try:
            await cl.disconnect()
        except Exception:
            pass
        return await wait.edit_text(f"❌ Could not send code: <code>{esc(str(e))[:200]}</code>")
    TG_PENDING[m.from_user.id] = {"client": cl, "phone": phone, "hash": sent.phone_code_hash,
                                  "iso": iso, "cname": cname}
    await state.set_state(TgAdd.code)
    how = {"SentCodeTypeApp": "the <b>Telegram app</b> (existing login)", "SentCodeTypeSms": "<b>SMS</b>",
           "SentCodeTypeCall": "a <b>phone call</b>", "SentCodeTypeFlashCall": "a flash call",
           "SentCodeTypeMissedCall": "a missed call"}.get(type(sent.type).__name__, "Telegram")
    await wait.edit_text(f"{iso_flag(iso)} <b>{esc(cname)}</b> · <code>{phone}</code>\n{BAR}\n"
                         f"{E('mail')} Login code sent via {how}.\n\n"
                         f"Send the code <b>with dashes or spaces</b>, e.g. <code>1-2-3-4-5</code>\n"
                         f"{E('low')} <i>If you send it as plain digits Telegram may cancel the code.</i>",
                         reply_markup=CANCEL_TG())


@admin.message(StateFilter(TgAdd.code))
async def tg_code(m: Message, state: FSMContext):
    p = TG_PENDING.get(m.from_user.id)
    if not p:
        await state.clear()
        return await m.answer("Session expired. Start again from TG Accounts → Add Account.")
    code = re.sub(r"\D", "", m.text or "")
    if len(code) < 4:
        return await m.answer("❗ Send the login code, e.g. <code>1-2-3-4-5</code>")
    try:
        await p["client"].sign_in(p["phone"], code, phone_code_hash=p["hash"])
    except tgerr.SessionPasswordNeededError:
        await state.set_state(TgAdd.pwd)
        return await m.answer(f"{E('key')} This account has <b>Two-Step Verification</b>.\n"
                              "Send its <b>password</b> (your message will be deleted):", reply_markup=CANCEL_TG())
    except tgerr.PhoneCodeInvalidError:
        return await m.answer("❌ Wrong code. Send it again (e.g. <code>1-2-3-4-5</code>).")
    except tgerr.PhoneCodeExpiredError:
        await tg_cleanup(m.from_user.id)
        await state.clear()
        return await m.answer("⌛ Code expired / was cancelled by Telegram.\nStart again and send the code like "
                              "<code>1-2-3-4-5</code>.", reply_markup=kb([B("Add Account", "adm:tgaadd", "x_add")]))
    except tgerr.FloodWaitError as e:
        return await m.answer(f"⏳ Flood-wait {e.seconds}s. Try again later.")
    except Exception as e:
        await tg_cleanup(m.from_user.id)
        await state.clear()
        return await m.answer(f"❌ Login failed: <code>{esc(str(e))[:200]}</code>")
    await tg_after_login(m, state, None)


@admin.message(StateFilter(TgAdd.pwd))
async def tg_pwd(m: Message, state: FSMContext):
    p = TG_PENDING.get(m.from_user.id)
    if not p:
        await state.clear()
        return await m.answer("Session expired. Start again.")
    pw = (m.text or "").strip()
    try:
        await m.delete()
    except Exception:
        pass
    if not pw:
        return await m.answer("❗ Send the password as text.")
    try:
        await p["client"].sign_in(password=pw)
    except tgerr.PasswordHashInvalidError:
        return await m.answer("❌ Wrong password. Send it again.")
    except tgerr.FloodWaitError as e:
        return await m.answer(f"⏳ Flood-wait {e.seconds}s. Try again later.")
    except Exception as e:
        await tg_cleanup(m.from_user.id)
        await state.clear()
        return await m.answer(f"❌ Login failed: <code>{esc(str(e))[:200]}</code>")
    await tg_after_login(m, state, pw)


async def tg_after_login(m: Message, state: FSMContext, pw):
    p = TG_PENDING[m.from_user.id]
    cl = p["client"]
    try:
        me = await cl.get_me()
        p["tgid"] = me.id
        p["name"] = f"{me.first_name or ''} {me.last_name or ''}".strip() or "-"
    except Exception:
        p["tgid"], p["name"] = 0, "-"
    p["session"] = cl.session.save()
    if pw:
        p["twofa"] = pw
        await state.set_state(TgAdd.price)
        return await m.answer(f"{E('ok')} <b>Logged in!</b> {esc(p['name'])}\n"
                              f"{E('key')} 2FA password saved.\n\n{E('money')} Now send the <b>price</b> ({CURRENCY}):",
                              reply_markup=CANCEL_TG())
    await state.set_state(TgAdd.setpwd)
    await m.answer(f"{E('ok')} <b>Logged in!</b> {esc(p['name'])}\n"
                   f"{E('low')} This account has <b>no Two-Step password</b>.\n\n"
                   f"{E('key')} Send a <b>new password</b> to set it now (min 4 chars), or send <code>skip</code> "
                   f"to sell without 2FA.\n<i>(your message will be deleted)</i>", reply_markup=CANCEL_TG())


@admin.message(StateFilter(TgAdd.setpwd))
async def tg_setpwd(m: Message, state: FSMContext):
    p = TG_PENDING.get(m.from_user.id)
    if not p:
        await state.clear()
        return await m.answer("Session expired. Start again.")
    txt = (m.text or "").strip()
    try:
        await m.delete()
    except Exception:
        pass
    if txt.lower() == "skip":
        p["twofa"] = ""
    else:
        if len(txt) < 4 or len(txt) > 60:
            return await m.answer("❗ Password must be 4–60 characters. Send again or <code>skip</code>.")
        try:
            await p["client"].edit_2fa(new_password=txt, hint="")
        except Exception as e:
            return await m.answer(f"❌ Could not set password: <code>{esc(str(e))[:200]}</code>\n"
                                  "Try another password or send <code>skip</code>.")
        p["twofa"] = txt
    await state.set_state(TgAdd.price)
    await m.answer(f"{E('ok')} {'Password set.' if p['twofa'] else 'No 2FA.'}\n\n"
                   f"{E('money')} Now send the <b>price</b> ({CURRENCY}):", reply_markup=CANCEL_TG())


@admin.message(StateFilter(TgAdd.price))
async def tg_price(m: Message, state: FSMContext):
    p = TG_PENDING.get(m.from_user.id)
    if not p or not p.get("session"):
        await state.clear()
        return await m.answer("Session expired. Start again.")
    try:
        price = float((m.text or "").replace(",", "").strip())
        if price < 0:
            raise ValueError
    except ValueError:
        return await m.answer("❗ Send a valid number.")
    aid = await nid("tgacc")
    D["tgacc"][str(aid)] = {"id": aid, "phone": p["phone"], "iso": p["iso"], "cname": p["cname"],
                            "flag": iso_flag(p["iso"]), "price": price, "twofa": p.get("twofa", ""),
                            "session": p["session"], "tgid": p.get("tgid", 0), "sold": 0, "active": 1,
                            "added_at": int(time.time())}
    await put("tgacc", aid)
    await tg_cleanup(m.from_user.id)          # disconnect only - the saved session stays valid
    await state.clear()
    await m.answer(f"{E('ok')} <b>Account added!</b>\n{BAR}\n"
                   f"{iso_flag(p['iso'])} {esc(p['cname'])} · <code>{esc(p['phone'])}</code>\n"
                   f"{E('key')} 2FA: <code>{esc(p.get('twofa') or 'none')}</code>\n"
                   f"{E('money')} Price: <b>{money(price)}</b>",
                   reply_markup=kb([B("Add Another", "adm:tgaadd", "x_add"), B("Accounts", "adm:tga:0", "x_nav")]))


# ═════════════ MAIN ═════════════
async def start_web():
    """Tiny HTTP endpoint so hosts that need an open port (Render web service) stay happy."""
    port = os.getenv("PORT")
    if not port:
        return
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Health endpoint on port %s", port)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty - nobody can open the admin panel!")
    if not TG_ON:
        log.warning("Telegram account selling disabled (need TG_API_ID, TG_API_HASH and `pip install telethon`)")
    await asyncio.to_thread(init_store)
    await seed_cats()
    await start_web()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    spam = AntiSpam()
    for obs in (dp.message, dp.callback_query):
        obs.outer_middleware(spam)
        obs.outer_middleware(Guard())
    dp.include_router(menu)
    dp.include_router(admin)
    dp.include_router(router)
    await bot.set_my_commands([BotCommand(command="start", description="Main menu"),
                               BotCommand(command="cancel", description="Cancel current action")])
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("%s bot started (Binance auto: %s, TG accounts: %s)", BRAND, BINANCE_ON, TG_ON)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass