# ═════════════════════════════════════════════════════════════════════════════
#  LIVE LOGIN + AUTO CODE-FORWARD
#  Admin logs the real Telegram account into the bot (Telethon). The session
#  stays connected. When the buyer later logs into that number on their own
#  phone, Telegram's official account (777000) sends the login code to the
#  number — since the bot is already signed in there, it SEES that message
#  and forwards the code straight to the buyer inside your shop bot.
#
#  Requires:  pip install telethon
#  Env vars:  TG_API_ID, TG_API_HASH   (get free from https://my.telegram.org)
#
#  Paste this file's content:
#   1) The imports + config block  -> near the top of your bot, after other env vars
#   2) Everything else             -> right after tg_accounts_patch.py's content
#  Then apply the 3 small edits at the bottom of this file.
# ═════════════════════════════════════════════════════════════════════════════
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (SessionPasswordNeededError, PhoneCodeInvalidError,
                              PhoneCodeExpiredError, FloodWaitError, PasswordHashInvalidError)

TG_API_ID = int(os.getenv("TG_API_ID", "0") or 0)
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_LIVE_ON = bool(TG_API_ID and TG_API_HASH)

LOGIN_CLIENTS: dict[int, TelegramClient] = {}     # admin_id -> temp client mid-login
FORWARDERS: dict[str, asyncio.Task] = {}          # phone -> running forwarder task
CODE_RE = re.compile(r"\b(\d{5,6})\b")


# ── parse extra fields back out of the stored account text (see tga_format) ──
def tga_field(data: str, label: str) -> str:
    for line in (data or "").splitlines():
        if label in line:
            return line.split(":", 1)[1].strip()
    return ""


def tga_sess_of(data: str) -> str:
    return tga_field(data, "🔑 Session")


def tga_order_buyer(data: str):
    o = next((o for o in D["orders"].values() if o["data"] == data), None)
    return o["user_id"] if o else None


# ── background forwarder: stays connected, watches for Telegram's 777000 code ──
async def _forward_loop(phone: str, session_str: str, buyer_id: int, bot: Bot):
    client = TelegramClient(StringSession(session_str), TG_API_ID, TG_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("TG session for %s is no longer authorized.", phone)
            return

        @client.on(events.NewMessage(from_users=777000))
        async def _on_code(event):
            m = CODE_RE.search(event.raw_text or "")
            if not m:
                return
            try:
                await bot.send_message(
                    buyer_id,
                    f"{E('ok')} <b>New login code received!</b>\n{BAR}\n"
                    f"📱 Account: <code>{esc(phone)}</code>\n"
                    f"🔑 Code: <code>{m.group(1)}</code>\n{BAR}\n"
                    f"<i>Enter this in Telegram to finish logging in.</i>")
            except Exception as e:
                log.warning("Could not deliver TG code to buyer %s: %s", buyer_id, e)

        log.info("Code-forwarder started for %s -> buyer %s", phone, buyer_id)
        await client.run_until_disconnected()
    except Exception as e:
        log.warning("Forwarder for %s crashed: %s", phone, e)
    finally:
        FORWARDERS.pop(phone, None)


def start_forwarder(phone: str, session_str: str, buyer_id: int, bot: Bot):
    if not TG_LIVE_ON or not session_str or phone in FORWARDERS:
        return
    FORWARDERS[phone] = asyncio.create_task(_forward_loop(phone, session_str, buyer_id, bot))


async def launch_all_forwarders(bot: Bot):
    """Call once at startup: resume forwarding for every already-sold TG account."""
    if not TG_LIVE_ON:
        return
    n = 0
    for s in D["stock"].values():
        p = prod(s["pid"])
        if not (p and p.get("tg") and s["sold"]):
            continue
        sess = tga_sess_of(s["data"])
        if not sess:
            continue
        buyer = tga_order_buyer(s["data"])
        if not buyer:
            continue
        start_forwarder(tga_phone_of(s["data"]), sess, buyer, bot)
        n += 1
    if n:
        log.info("Resumed %s TG code-forwarder(s).", n)


# ═════════════ Admin live-login wizard (hooks into TgAdd) ═════════════
class TgLogin(StatesGroup):
    code = State(); pwd2 = State()


@admin.callback_query(F.data == "adm:tga:login")
async def tga_login_start(c: CallbackQuery, state: FSMContext):
    if not TG_LIVE_ON:
        return await c.answer("TG_API_ID / TG_API_HASH not set in env.", show_alert=True)
    d = await state.get_data()
    phone = d.get("phone")
    if not phone:
        return await c.answer("Phone missing, restart.", show_alert=True)
    await c.answer("Sending code…")
    client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except FloodWaitError as e:
        await client.disconnect()
        return await show(c, f"{E('no')} Telegram flood-limited this number. Try again in {e.seconds}s.",
                          kb([B("Back", "adm:tga:home", "x_nav")]))
    except Exception as e:
        await client.disconnect()
        return await show(c, f"{E('no')} Could not send code: <code>{esc(str(e))}</code>",
                          kb([B("Back", "adm:tga:home", "x_nav")]))
    LOGIN_CLIENTS[c.from_user.id] = client
    await state.update_data(phone_code_hash=sent.phone_code_hash)
    await state.set_state(TgLogin.code)
    await show(c, f"✈️ <b>Logging in {esc(phone)}</b>\n{BAR}\n"
                  "A login code was just sent to this number by Telegram.\n"
                  "📩 Open that number's Telegram/SMS and send me the code here:",
               kb([B("Cancel", "adm:tga:home", "x_no")]))


@admin.message(StateFilter(TgLogin.code))
async def tga_login_code(m: Message, state: FSMContext):
    client = LOGIN_CLIENTS.get(m.from_user.id)
    d = await state.get_data()
    if not client or not d.get("phone"):
        await state.clear()
        return await m.answer("Session expired. Start again from TG Accounts.")
    code = re.sub(r"\D", "", m.text or "")
    if not code:
        return await m.answer("❗ Send the numeric code Telegram sent you.")
    try:
        await client.sign_in(phone=d["phone"], code=code, phone_code_hash=d.get("phone_code_hash"))
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        return await m.answer("❗ Wrong or expired code. Check and send again, or /cancel")
    except SessionPasswordNeededError:
        await state.set_state(TgLogin.pwd2)
        return await m.answer("🔐 This account has <b>2-Step Verification</b>.\nSend its password:",
                              reply_markup=kb([B("Cancel", "adm:tga:home", "x_no")]))
    except Exception as e:
        return await m.answer(f"❗ Login failed: <code>{esc(str(e))}</code>\nSend the code again or /cancel")
    await _finish_login(m, state, client)


@admin.message(StateFilter(TgLogin.pwd2))
async def tga_login_pwd(m: Message, state: FSMContext):
    client = LOGIN_CLIENTS.get(m.from_user.id)
    if not client:
        await state.clear()
        return await m.answer("Session expired. Start again.")
    pwd = (m.text or "").strip()
    try:
        await client.sign_in(password=pwd)
    except PasswordHashInvalidError:
        return await m.answer("❗ Wrong password. Try again or /cancel")
    except Exception as e:
        return await m.answer(f"❗ Login failed: <code>{esc(str(e))}</code>")
    await state.update_data(pwd=pwd)      # save the 2FA password for delivery to the buyer
    await _finish_login(m, state, client)


async def _finish_login(ev, state: FSMContext, client: TelegramClient):
    uid = ev.from_user.id
    sess_str = client.session.save()
    await client.disconnect()
    LOGIN_CLIENTS.pop(uid, None)
    d = await state.get_data()
    await state.update_data(sess=sess_str, pwd=d.get("pwd", ""))
    try:
        await (ev.message if isinstance(ev, CallbackQuery) else ev).answer(
            f"{E('ok')} <b>Logged in!</b> The bot will auto-forward future login codes to the buyer.")
    except Exception:
        pass
    await tga_next(ev, state)


# ── 3 edits to wire this into tg_accounts_patch.py ──
#
# 1) In tga_next(), replace the "if 'sess' not in d:" block with:
#
#    if "sess" not in d:
#        await state.set_state(TgLogin.code if False else TgAdd.sess)  # placeholder, see below
#        rows = [[B("Login Now (recommended)", "adm:tga:login", "x_ok", fb="🔐")]] if TG_LIVE_ON else []
#        rows.append([B("Paste Session Manually", "adm:tga:pastesess", "x_edit", fb="🔑")])
#        rows.append([B("Skip", "adm:tga:skip", "skip")])
#        rows.append(cancel)
#        return await show(ev, card + "3️⃣ Choose how to provide the session.", kb(*rows))
#
#    (then add a callback "adm:tga:pastesess" -> state.set_state(TgAdd.sess); show(...) same as the old prompt)
#
# 2) In cb_buyok (after the invoice is shown / notify_admins), add:
#
#    if p.get("tg"):
#        sess = tga_sess_of(o["data"])
#        if sess:
#            start_forwarder(tga_phone_of(o["data"]), sess, uid, c.bot)
#
# 3) On bot startup (wherever you call dp.start_polling / your main()), add BEFORE polling starts:
#
#    await launch_all_forwarders(bot)