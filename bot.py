"""
Telegram bot: the user-facing state machine.

Flow:
  /start -> DISCLOSURE -> SELECT_PLATFORM -> ENTER_ID -> verify -> branch
"""
import datetime as dt
import logging
import sys
import warnings
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ChatMemberHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters,
)

from app import config
from app.config import PLATFORMS, WELCOME_TEXT
from app.database import SessionLocal, init_db
from app import verification as V

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,          # stdout so Railway shows these as info, not red errors
)
# Keep Railway logs clean: silence per-poll HTTP spam (which also prints the bot token),
# scheduler chatter, and the two harmless PTB conversation warnings.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)
warnings.filterwarnings("ignore", message="If 'per_message=False'")
warnings.filterwarnings("ignore", message="No `JobQueue` set up")

log = logging.getLogger("bot")

# Conversation states
SELECT_PLATFORM, ENTER_ID = range(2)


# ---------- keyboards ----------
def platform_keyboard():
    rows = [[InlineKeyboardButton(f"🔹 {cfg['display_name']}", callback_data=f"platform:{key}")]
            for key, cfg in PLATFORMS.items()]
    return InlineKeyboardMarkup(rows)


def retry_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Try Again", callback_data="retry")],
        [InlineKeyboardButton("🆘 Contact Support", url=config.SUPPORT_URL)],
    ])


def referral_keyboard(platform_key):
    cfg = PLATFORMS[platform_key]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➕ {cfg['display_name']} account banao", url=cfg["referral_link"])],
        [InlineKeyboardButton("🔄 Ho gaya — Try Again", callback_data="retry")],
        [InlineKeyboardButton("🆘 Contact Support", url=config.SUPPORT_URL)],
    ])


def _referral_prompt_text(platform_key):
    d = PLATFORMS[platform_key]["display_name"]
    return (
        f"❌ Ye {d} ID humare records me nahi hai.\n\n"
        f"Community join karne ke liye neeche button se apna {d} account banao 👇, "
        f'phir "Try Again" dabao. 🔄'
    )


# ---------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from app import admin
    u = update.effective_user
    # Admins get the admin menu, not the user verification flow.
    if admin._is_admin(u.id):
        await update.effective_message.reply_text(admin.ADMIN_HELP_TEXT)
        return ConversationHandler.END
    await update.effective_message.reply_text(WELCOME_TEXT, reply_markup=platform_keyboard())
    # Lightweight usage log for the daily digest (never blocks the welcome).
    try:
        from app.models import VerificationLog
        s = SessionLocal()
        s.add(VerificationLog(telegram_user_id=u.id, result="START"))
        s.commit()
        s.close()
    except Exception:  # noqa: BLE001
        log.debug("start log failed", exc_info=True)
    return SELECT_PLATFORM


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record joins/leaves for the community channel (needs the bot to be a channel admin)."""
    cmu = update.chat_member
    if cmu is None or not config.COMMUNITY_CHAT_ID:
        return
    if str(cmu.chat.id) != str(config.COMMUNITY_CHAT_ID):
        return
    old = cmu.old_chat_member.status
    new = cmu.new_chat_member.status
    present = ("member", "administrator", "creator")
    gone = ("left", "kicked")
    joined = new in present and old not in present
    left = new in gone and old in present
    if not (joined or left):
        return
    try:
        from app.models import ChannelEvent
        s = SessionLocal()
        s.add(ChannelEvent(
            telegram_user_id=cmu.new_chat_member.user.id,
            event="joined" if joined else "left",
        ))
        s.commit()
        s.close()
    except Exception:  # noqa: BLE001
        log.debug("channel event log failed", exc_info=True)


async def on_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":", 1)[1]
    cfg = PLATFORMS.get(key)
    if not cfg:
        await q.edit_message_text("Ye platform samajh nahi aaya. /start dobara bhejo. 🙏")
        return ConversationHandler.END
    context.user_data["platform"] = key
    await q.edit_message_text(
        f"Apni {cfg['uuid_label']} enter karo 👇\n\nExample: {cfg['example']}"
    )
    return ENTER_ID


async def on_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Apna broker select karo 👇", reply_markup=platform_keyboard())
    return SELECT_PLATFORM


async def on_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("platform")
    if not key:
        await update.message.reply_text("Please /start dobara bhejo. 🙏")
        return ConversationHandler.END

    raw = update.message.text
    tg_user = update.effective_user
    await update.message.reply_text("🔍 Verify kar rahe hain... ⏳")

    session = SessionLocal()
    try:
        result = V.verify(session, key, raw, tg_user)
    except Exception:
        log.exception("verify failed")
        session.rollback()
        await update.message.reply_text(
            "Kuch technical issue aa gaya. Thodi der baad dobara try karo. 🙏",
            reply_markup=retry_keyboard(),
        )
        return SELECT_PLATFORM
    finally:
        session.close()

    return await _respond(update, context, result, tg_user)


async def _respond(update, context, result: V.Result, tg_user):
    cfg = PLATFORMS.get(result.platform_key, {})

    if result.status == V.VERIFIED:
        await _grant_access(update, context)
        return ConversationHandler.END

    if result.status == V.INVALID_FORMAT:
        await update.message.reply_text(
            f"🤔 Ye sahi {cfg.get('uuid_label', 'ID')} nahi lag rahi. "
            f"Check karke dobara enter karo (example: {cfg.get('example', '')}).",
        )
        return ENTER_ID

    if result.status == V.RATE_LIMITED:
        await update.message.reply_text(
            "⏳ Bahut baar try kiya hai. Thodi der baad dobara try karo. 🙏",
        )
        return SELECT_PLATFORM

    if result.status == V.DUPLICATE:
        # Never reveal who holds it.
        await update.message.reply_text(
            "⚠️ Is ID se access nahi mil paya. Humari team ko inform kar diya hai, "
            "wo jaldi ise sort kar denge. 🙏",
            reply_markup=retry_keyboard(),
        )
        await _alert_admin_duplicate(context, tg_user, result)
        return SELECT_PLATFORM

    if result.status == V.NOT_FOUND:
        if result.not_found_action == "REMAP_TEMPLATE":
            await update.message.reply_text(
                _shark_remap_message(tg_user, result.normalized_uuid),
                reply_markup=retry_keyboard(),
            )
        elif result.not_found_action == "REFERRAL_PROMPT":
            await update.message.reply_text(
                _referral_prompt_text(result.platform_key),
                reply_markup=referral_keyboard(result.platform_key),
            )
            await _alert_admin_not_found(context, tg_user, result)
        else:  # plain ADMIN_ALERT fallback
            await update.message.reply_text(
                "Is ID ko abhi verify nahi kar paye. Humari team ko inform kar diya hai, "
                "wo aapko jaldi set up karegi. 🙏",
                reply_markup=retry_keyboard(),
            )
            await _alert_admin_not_found(context, tg_user, result)
        return SELECT_PLATFORM

    await update.message.reply_text("Kuch gadbad ho gayi. /start dobara bhejo. 🙏")
    return ConversationHandler.END


async def _grant_access(update, context):
    # Path 1: a fixed community invite link is configured -> send it with the custom message.
    # Works even if the bot can't create links yet. Downside: a fixed link is shareable, so it
    # does not stop a verified user from forwarding it to someone who never verified.
    if config.COMMUNITY_INVITE_LINK:
        text = config.COMMUNITY_SUCCESS_TEXT.replace("{invite_link}", config.COMMUNITY_INVITE_LINK)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🚀 JOIN PRIVATE COMMUNITY", url=config.COMMUNITY_INVITE_LINK)]])
        await update.message.reply_text(text, reply_markup=kb, disable_web_page_preview=True)
        return

    # Path 2: no fixed link -> generate a single-use, 24h link (needs the bot to be an admin
    # of a SUPERGROUP). More secure: each verified user gets their own one-time link.
    chat_id = config.COMMUNITY_CHAT_ID
    if not chat_id:
        await update.message.reply_text(
            "✅ Verified! Access set up ho raha hai — admin aapko jaldi invite bhejega. 🙏"
        )
        log.warning("No COMMUNITY_INVITE_LINK and no COMMUNITY_CHAT_ID set.")
        return
    try:
        expire = dt.datetime.utcnow() + dt.timedelta(hours=24)
        invite = await context.bot.create_chat_invite_link(
            chat_id=int(chat_id), member_limit=1, expire_date=expire,
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🚀 JOIN PRIVATE COMMUNITY", url=invite.invite_link)]])
        await update.message.reply_text(
            "✅ Verified! 🎉\n\nAapka account verify ho gaya. Community join karne ke liye "
            "neeche tap karo 👇\n\n(Ye link sirf ek baar chalega, 24 hours me expire ho jayega.)",
            reply_markup=kb,
        )
    except Exception:
        log.exception("invite link creation failed")
        await update.message.reply_text(
            "✅ Verified! Access set up ho raha hai — admin aapko jaldi invite bhejega. 🙏"
        )


def _shark_remap_message(tg_user, shark_uuid: str) -> str:
    tmpl = config.SHARK_REMAP_TEMPLATE_ENV
    if not tmpl:
        try:
            with open("shark_remap_template.txt", "r", encoding="utf-8") as f:
                tmpl = f.read()
        except FileNotFoundError:
            tmpl = (
                "We couldn't find your Shark account in our records. A remap is required.\n"
                "[Paste your approved Shark remap instructions here.]\n\n"
                "Reference — Telegram: @{telegram_username} (id {telegram_user_id}), "
                "Shark ID: {shark_uuid}, Date: {date}"
            )
    return (tmpl
            .replace("\\n", "\n")
            .replace("{telegram_username}", tg_user.username or "N/A")
            .replace("{telegram_user_id}", str(tg_user.id))
            .replace("{shark_uuid}", shark_uuid or "N/A")
            .replace("{date}", dt.date.today().isoformat()))


async def _alert_admin_not_found(context, tg_user, result):
    if not config.ADMIN_ALERT_CHAT_ID:
        return
    name = PLATFORMS[result.platform_key]["display_name"]
    msg = (
        f"🚨 {name.upper()} REMAP REQUIRED\n\n"
        f"Telegram User: @{tg_user.username or 'N/A'}\n"
        f"Telegram ID: {tg_user.id}\n"
        f"Submitted {name} UUID: {result.normalized_uuid}\n"
        f"Status: UUID NOT FOUND\n\n"
        f"Action Required: Remap / verify user manually."
    )
    await context.bot.send_message(chat_id=int(config.ADMIN_ALERT_CHAT_ID), text=msg)


async def _alert_admin_duplicate(context, tg_user, result):
    if not config.ADMIN_ALERT_CHAT_ID:
        return
    name = PLATFORMS[result.platform_key]["display_name"]
    msg = (
        f"⚠️ DUPLICATE ID ATTEMPT ({name})\n\n"
        f"Telegram User: @{tg_user.username or 'N/A'}\n"
        f"Telegram ID: {tg_user.id}\n"
        f"Submitted UUID: {result.normalized_uuid}\n"
        f"Status: ALREADY LINKED to another Telegram account.\n\n"
        f"Action Required: Verify and remap manually if legitimate."
    )
    await context.bot.send_message(chat_id=int(config.ADMIN_ALERT_CHAT_ID), text=msg)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Cancel ho gaya. Dobara start karne ke liye /start bhejo. 🙏")
    return ConversationHandler.END


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log errors as one clean line instead of a full traceback."""
    from telegram.error import Conflict, NetworkError
    err = context.error
    if isinstance(err, Conflict):
        log.warning("409 Conflict: another instance is polling this bot token. "
                    "Make sure only ONE deployment is running.")
    elif isinstance(err, NetworkError):
        log.warning("Network hiccup talking to Telegram: %s", err)
    else:
        log.error("Handler error: %s", err)


def build_application() -> Application:
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Put it in your .env file.")
    init_db()
    app = Application.builder().token(config.BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_PLATFORM: [
                CallbackQueryHandler(on_platform, pattern="^platform:"),
                CallbackQueryHandler(on_retry, pattern="^retry$"),
            ],
            ENTER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_id)],
        },
        fallbacks=[CommandHandler("start", start), CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_error_handler(on_error)
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER), group=2)

    # Admin operations (Excel/CSV import, sync commands) in the private admin group.
    from app import admin
    admin.register(app)

    # Automatic broker-ID sync inside this same process (startup + hourly).
    from app import sync
    jq = app.job_queue
    if jq is not None:
        jq.run_once(sync.sync_job, when=15)
        jq.run_repeating(sync.sync_job, interval=config.SYNC_INTERVAL_SECONDS,
                         first=config.SYNC_INTERVAL_SECONDS)
        # Daily digest + auto-backup at 11:30 PM IST.
        jq.run_daily(admin.digest_job, time=dt.time(23, 30, tzinfo=ZoneInfo("Asia/Kolkata")))
        log.info("Broker sync scheduled: startup + every %ss; digest at 23:30 IST",
                 config.SYNC_INTERVAL_SECONDS)
    else:
        log.warning("JobQueue unavailable — install python-telegram-bot[job-queue] for auto-sync.")
    return app


def main():
    app = build_application()
    log.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
