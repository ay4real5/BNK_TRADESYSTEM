"""
Telegram bot setup and callback handlers.

Registers all command handlers and inline button callbacks.
"""

from __future__ import annotations

from loguru import logger
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from ..config import settings
from ..data import storage
from ..domain.enums import Mode, SignalStatus
from ..domain.models import TradeIdea
from ..execution.paper import PaperExecutor
from ..execution.safeguards import run_all_safeguards
from ..services import locks
from ..telegram import commands, formatters
from ..telegram.keyboards import confirm_live_keyboard, trade_confirmation_keyboard


_app: Application | None = None
_paper_executor = PaperExecutor()


# ---------------------------------------------------------------------------
# Signal notification (called by analyzer)
# ---------------------------------------------------------------------------

async def notify_signal(idea: TradeIdea) -> None:
    """Send a trade signal alert to all admin chat IDs."""
    if _app is None:
        return
    text = formatters.format_signal(idea)
    keyboard = trade_confirmation_keyboard(idea.id or 0)

    for chat_id in settings.admin_chat_ids:
        try:
            await _app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.error("Failed to send signal to {}: {}", chat_id, exc)


async def send_eod_report(summary: dict) -> None:
    """Send end-of-day report to all admin chat IDs."""
    if _app is None:
        return
    text = formatters.format_daily_report(summary)
    for chat_id in settings.admin_chat_ids:
        try:
            await _app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as exc:
            logger.error("Failed to send EOD report to {}: {}", chat_id, exc)


# ---------------------------------------------------------------------------
# Inline button callback handlers
# ---------------------------------------------------------------------------

async def handle_trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle confirm / reject / snooze button presses on trade signals."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "trade":
        return

    action = parts[1]
    try:
        signal_id = int(parts[2])
    except ValueError:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    from ..telegram.auth import is_admin
    if not is_admin(user_id):
        await query.edit_message_text("⛔ Not authorised.")
        return

    if action == "confirm":
        await _handle_confirm(query, signal_id)
    elif action == "reject":
        await storage.update_signal_status(signal_id, SignalStatus.REJECTED)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"❌ Signal #{signal_id} rejected.")
    elif action == "snooze":
        await storage.update_signal_status(signal_id, SignalStatus.SNOOZED)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"⏰ Signal #{signal_id} snoozed for {settings.signal_snooze_minutes} minutes."
        )


async def _handle_confirm(query, signal_id: int) -> None:
    """Process a confirmed signal based on current mode."""
    signals = await storage.get_recent_signals(limit=50)
    idea = next((s for s in signals if s.id == signal_id), None)

    if idea is None:
        await query.edit_message_text("⚠️ Signal not found.")
        return

    await storage.update_signal_status(signal_id, SignalStatus.CONFIRMED)
    await query.edit_message_reply_markup(reply_markup=None)

    mode = settings.mode

    if mode == Mode.ASSIST:
        await query.message.reply_text(
            f"✅ *Signal #{signal_id} confirmed \\(ASSIST\\)*\n\n"
            f"Entry plan logged\\. Execute manually on your broker\\.\n"
            f"Entry: `{idea.entry}` | SL: `{idea.sl}` | TP: `{idea.tp}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif mode == Mode.PAPER:
        try:
            state = await locks.check_can_trade()
        except Exception as exc:
            await query.message.reply_text(f"🚫 Cannot open paper trade: {exc}")
            return

        trade = await _paper_executor.open_trade(idea)
        trade.signal_id = signal_id
        trade_id = await storage.save_trade(trade)
        trade.id = trade_id
        await query.message.reply_text(
            f"📋 *PAPER trade opened* \\(ID: {trade_id}\\)\n"
            f"{idea.symbol.value} {idea.side.value.upper()} @ `{idea.entry}`\n"
            f"SL: `{idea.sl}` | TP: `{idea.tp}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif mode == Mode.LIVE:
        await query.message.reply_text(
            "⚡ LIVE execution is not yet fully implemented\\. "
            "Please place the trade manually on cTrader\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def handle_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle mode selection callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    parts = data.split(":")

    user_id = update.effective_user.id if update.effective_user else 0
    from ..telegram.auth import is_admin
    if not is_admin(user_id):
        await query.edit_message_text("⛔ Not authorised.")
        return

    if parts == ["mode", "live", "confirm"]:
        settings.mode = Mode.LIVE
        await query.edit_message_text("⚡ LIVE mode enabled. Real trades will be placed.")
    elif parts == ["mode", "live", "cancel"]:
        await query.edit_message_text("Cancelled.")
    elif len(parts) == 2 and parts[0] == "mode":
        try:
            settings.mode = Mode(parts[1])
            await query.edit_message_text(f"✅ Mode set to {parts[1].upper()}")
        except ValueError:
            await query.edit_message_text("❌ Invalid mode.")


# ---------------------------------------------------------------------------
# Bot factory
# ---------------------------------------------------------------------------

def build_application() -> Application:
    """Build and configure the Telegram Application."""
    global _app

    app = Application.builder().token(settings.telegram_bot_token).build()

    # Core commands
    app.add_handler(CommandHandler("start", commands.cmd_start))
    app.add_handler(CommandHandler("help", commands.cmd_help))
    app.add_handler(CommandHandler("status", commands.cmd_status))
    app.add_handler(CommandHandler("analyze", commands.cmd_analyze))

    # Analysis info
    app.add_handler(CommandHandler("signals", commands.cmd_signals))
    app.add_handler(CommandHandler("bias", commands.cmd_bias))
    app.add_handler(CommandHandler("levels", commands.cmd_levels))

    # Mode control
    app.add_handler(CommandHandler("mode", commands.cmd_mode))
    app.add_handler(CommandHandler("live_on", commands.cmd_live_on))
    app.add_handler(CommandHandler("live_off", commands.cmd_live_off))
    app.add_handler(CommandHandler("paper_on", commands.cmd_paper_on))
    app.add_handler(CommandHandler("paper_off", commands.cmd_paper_off))

    # Risk controls
    app.add_handler(CommandHandler("risk", commands.cmd_risk))
    app.add_handler(CommandHandler("limits", commands.cmd_limits))
    app.add_handler(CommandHandler("cooldown", commands.cmd_cooldown))

    # Safety
    app.add_handler(CommandHandler("pause", commands.cmd_pause))
    app.add_handler(CommandHandler("resume", commands.cmd_resume))
    app.add_handler(CommandHandler("kill", commands.cmd_kill))
    app.add_handler(CommandHandler("unlock", commands.cmd_unlock))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(handle_trade_callback, pattern=r"^trade:"))
    app.add_handler(CallbackQueryHandler(handle_mode_callback, pattern=r"^mode:"))

    _app = app
    logger.info("Telegram bot application built with {} handlers", len(app.handlers))
    return app
