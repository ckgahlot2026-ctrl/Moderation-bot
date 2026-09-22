"""
Telegram Group Moderation Bot
-----------------------------
Features:
  - Deletes links and abusive language
  - Warn -> Mute -> Ban escalation (3 warnings limit)
  - Admin commands: /warn, /mute, /unmute, /ban, /unban, /kick, /warnings, /clearwarn, /rules
"""

import os
import re
import sqlite3
import datetime as dt
import logging

from telegram import ChatPermissions, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from better_profanity import profanity
    profanity.load_censor_words()
    HAS_PROFANITY = True
except ImportError:
    HAS_PROFANITY = False

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "")
WARN_LIMIT = int(os.environ.get("WARN_LIMIT", "3"))
MUTE_MINUTES = int(os.environ.get("MUTE_MINUTES", "60"))
DB_PATH = os.environ.get("DB_PATH", "warnings.db")

WELCOME_MESSAGE = (
    "👋 Welcome, {first_name}!\n"
    "Please read the /rules before chatting."
)

RULES_TEXT = (
    "📌 <b>Group Rules</b>\n"
    "🚫 No links\n"
    "🚫 No abusive language\n"
    "✅ Be respectful\n"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("moderator-bot")

_URL_RE = re.compile(r"(https?://\S+)|(\bwww\.\S+)|(\bt\.me/\S+)|(\btelegram\.me/\S+)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# SQLITE-BACKED WARNING STORAGE
# (replaces the old in-memory dict so counts survive bot restarts)
# ---------------------------------------------------------------------------
_db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_db_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS warnings (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    )
    """
)
_db_conn.commit()


def get_warning_count(chat_id: int, user_id: int) -> int:
    row = _db_conn.execute(
        "SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    ).fetchone()
    return row[0] if row else 0


def increment_warning_count(chat_id: int, user_id: int) -> int:
    _db_conn.execute(
        """
        INSERT INTO warnings (chat_id, user_id, count) VALUES (?, ?, 1)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET count = count + 1
        """,
        (chat_id, user_id),
    )
    _db_conn.commit()
    return get_warning_count(chat_id, user_id)


def clear_warning_count(chat_id: int, user_id: int) -> None:
    _db_conn.execute("DELETE FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    _db_conn.commit()


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await is_admin(context, update.effective_chat.id, update.effective_user.id):
        return True
    await update.message.reply_text("⚠️ Admins only.")
    return False


def get_target(update: Update):
    if update.message and update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    return None


async def send_log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not LOG_CHANNEL_ID:
        return
    try:
        await context.bot.send_message(chat_id=int(LOG_CHANNEL_ID), text=text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def punish(update: Update, context: ContextTypes.DEFAULT_TYPE, user, reason: str) -> None:
    chat_id = update.effective_chat.id
    count = increment_warning_count(chat_id, user.id)

    if count >= WARN_LIMIT:
        try:
            await context.bot.ban_chat_member(chat_id, user.id)
            msg = f"🚫 {user.mention_html()} banned after {count} violations ({reason})."
        except Exception as e:
            msg = f"Ban failed: {e}"
    elif count == WARN_LIMIT - 1:
        until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=MUTE_MINUTES)
        try:
            await context.bot.restrict_chat_member(
                chat_id, user.id, permissions=ChatPermissions(can_send_messages=False), until_date=until
            )
            msg = f"🔇 {user.mention_html()} muted {MUTE_MINUTES}m (warning {count}/{WARN_LIMIT}, {reason})."
        except Exception as e:
            msg = f"Mute failed: {e}"
    else:
        msg = f"⚠️ {user.mention_html()} warning {count}/{WARN_LIMIT} ({reason})."

    await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML)
    await send_log(context, f"Chat {chat_id}: {msg}")


# ---------------------------------------------------------------------------
# MODERATION LOGIC
# ---------------------------------------------------------------------------
async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or user.is_bot:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    if await is_admin(context, update.effective_chat.id, user.id):
        return

    text = message.text or message.caption or ""
    if not text:
        return

    reason = None
    if _URL_RE.search(text):
        reason = "posting a link"
    elif HAS_PROFANITY and profanity.contains_profanity(text):
        reason = "abusive language"

    if reason:
        try:
            await message.delete()
        except Exception:
            pass
        await punish(update, context, user, reason)


async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if result is None:
        return
    old, new = result.old_chat_member.status, result.new_chat_member.status
    if old in ("left", "kicked") and new in ("member", "restricted"):
        u = result.new_chat_member.user
        if not u.is_bot:
            await context.bot.send_message(
                update.effective_chat.id,
                WELCOME_MESSAGE.format(first_name=u.first_name or "there"),
            )


# ---------------------------------------------------------------------------
# COMMAND HANDLERS
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot active. Send /rules to view group guidelines.")


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES_TEXT, parse_mode=ParseMode.HTML)


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target = get_target(update)
    if not target:
        await update.message.reply_text("Reply to a user's message with /warn.")
        return
    await punish(update, context, target, "manual warning")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target = get_target(update)
    if not target:
        await update.message.reply_text("Reply to a user's message with /mute [minutes].")
        return
    minutes = int(context.args[0]) if context.args and context.args[0].isdigit() else MUTE_MINUTES
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=until
        )
        await update.message.reply_text(f"🔇 Muted {target.mention_html()} for {minutes}m.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target = get_target(update)
    if not target:
        await update.message.reply_text("Reply to a user's message with /unmute.")
        return
    try:
        full_permissions = ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        )
        await context.bot.restrict_chat_member(update.effective_chat.id, target.id, permissions=full_permissions)
        await update.message.reply_text(f"🔊 Unmuted {target.mention_html()}.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target = get_target(update)
    if not target:
        await update.message.reply_text("Reply to a user's message with /ban.")
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text(f"🚫 Banned {target.mention_html()}.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, int(context.args[0]))
        await update.message.reply_text("✅ User unbanned.")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target = get_target(update)
    if not target:
        await update.message.reply_text("Reply to a user's message with /kick.")
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text(f"👢 Kicked {target.mention_html()}.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def cmd_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = get_target(update) or update.effective_user
    count = get_warning_count(update.effective_chat.id, target.id)
    await update.message.reply_text(f"{target.mention_html()} has {count}/{WARN_LIMIT} warning(s).", parse_mode=ParseMode.HTML)


async def cmd_clearwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target = get_target(update)
    if not target:
        await update.message.reply_text("Reply to a user's message with /clearwarn.")
        return
    clear_warning_count(update.effective_chat.id, target.id)
    await update.message.reply_text(f"✅ Warnings cleared for {target.mention_html()}.", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
        raise SystemExit("Error: BOT_TOKEN is missing!")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("warnings", cmd_warnings))
    app.add_handler(CommandHandler("clearwarn", cmd_clearwarn))

    app.add_handler(ChatMemberHandler(welcome, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & filters.ChatType.GROUPS & ~filters.COMMAND, moderate))

    logger.info("Starting bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
