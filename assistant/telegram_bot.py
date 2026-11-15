"""Telegram front-end (optional). Needs TELEGRAM_BOT_TOKEN and an AI API key.
  python -m assistant.telegram_bot
Send any question as a message. Follow-ups ("and last week?") use the last few questions of
the same chat. /new starts a fresh conversation; /summary sends the daily summary.

Access: set TELEGRAM_ALLOWED_USER_IDS=123,456 (numeric Telegram user ids) so only those people
can query the data. Anyone can find a bot by its username, so leave this empty only for a demo.
"""
import asyncio
import os
from functools import partial

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .llm import answer_question, make_llm
from .memory import ConversationMemory

TELEGRAM_MAX_CHARS = 4096          # Telegram rejects longer messages
memory = ConversationMemory()      # keyed by Telegram chat id
llm = None                         # created in main(), so importing this module needs no API key


def allowed_user_ids() -> set:
    raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}


def is_allowed(user_id: int) -> bool:
    ids = allowed_user_ids()
    return not ids or user_id in ids          # empty list = demo mode (warned about at start-up)


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS - 96) -> list:
    """Split a long reply into Telegram-sized parts, preferring line breaks."""
    parts, text = [], text or "(empty answer)"
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    return parts + [text]


async def reply(update: Update, text: str) -> None:
    for part in split_message(text):
        await update.message.reply_text(part)


async def guard_access(update: Update) -> bool:
    if is_allowed(update.effective_user.id):
        return True
    await update.message.reply_text("Sorry, you are not authorised to use this assistant.")
    return False


async def on_question(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_access(update):
        return
    user = str(update.effective_user.id)
    cid = f"tg-{update.effective_chat.id}"
    history = memory.history(cid)
    # answer_question is blocking (network + DB), so run it off the event loop
    a = await asyncio.to_thread(partial(answer_question, update.message.text, llm, f"telegram:{user}",
                                        history=history, conversation=cid))
    if a.ok:
        memory.add(cid, a.question, a.sql)
    await reply(update, a.text)


async def on_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_access(update):
        return
    memory.clear(f"tg-{update.effective_chat.id}")
    await reply(update, "New conversation started. Ask me anything about the data.")


async def on_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_access(update):
        return
    from daily_summary import build_summary
    await reply(update, await asyncio.to_thread(build_summary, "2026-10-31"))


def main() -> None:
    global llm
    llm = make_llm()
    if not allowed_user_ids():
        print("WARNING: TELEGRAM_ALLOWED_USER_IDS is not set, so ANYONE who finds this bot can query "
              "the data. Fine for a demo; set it before real use.")
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("summary", on_summary))
    app.add_handler(CommandHandler("new", on_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_question))
    app.run_polling()


if __name__ == "__main__":
    main()
