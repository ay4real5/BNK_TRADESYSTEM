"""
Telegram command handlers.

Implements all commands specified in the product spec.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..config import settings
from ..data import storage
from ..domain.enums import Mode, SignalStatus
from ..domain.errors import LockError
from ..services import analyzer, locks
from ..telegram.auth import admin_only, is_admin, verify_pin
from ..telegram.formatters import (
    HELP_TEXT,
    format_daily_report,
    format_recent_signals,
    format_status,
)


# Track last scan time globally
_last_scan_time: datetime | None = None


def set_last_scan_time(ts: datetime) -> None:
    global _last_scan_time
    _last_scan_time = ts


# ---------------------------------------------------------------------------
# Core commands
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await locks.get_state()
    text = format_status(state, last_scan=_last_scan_time)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Running analysis scan\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    try:
        ideas = await analyzer.run_analysis_cycle()
        set_last_scan_time(datetime.utcnow())
        if not ideas:
            await update.message.reply_text(
                "🔎 No setups found at this time\\. Markets scanned\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await update.message.reply_text(
                f"✅ Found *{len(ideas)}* setup\\(s\\)\\. Alerts sent\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    except Exception as exc:
        logger.error("Manual analyze error: {}", exc)
        await update.message.reply_text(f"❌ Analysis error: {exc}")


# ---------------------------------------------------------------------------
# Analysis info commands
# ---------------------------------------------------------------------------

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    signals = await storage.get_recent_signals(limit=10)
    text = format_recent_signals(signals)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_bias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current 1H bias for both symbols."""
    from ..data import market_data
    from ..strategy.rules import determine_bias
    from ..strategy.features import add_all_features
    from ..data.market_data import candles_to_df

    lines = ["*📊 1H Bias Summary*", ""]
    for symbol in settings.active_symbols:
        try:
            candles = await market_data.fetch_candles(symbol, settings.bias_tf, count=300)
            df = candles_to_df(candles)
            df = add_all_features(df)
            bias = determine_bias(df)
            price = candles[-1].close
            emoji = "📈" if bias.value == "bullish" else "📉" if bias.value == "bearish" else "➡️"
            lines.append(
                f"{emoji} *{symbol.value}*: {bias.value.capitalize()} \\@ `{price:.2f}`"
            )
        except Exception as exc:
            lines.append(f"⚠️ {symbol.value}: Error — {exc}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_levels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show daily high/low levels."""
    from ..data import market_data
    from ..data.market_data import candles_to_df

    lines = ["*📐 Key Price Levels*", ""]
    for symbol in settings.active_symbols:
        try:
            candles = await market_data.fetch_candles(symbol, "1d", count=5)
            df = candles_to_df(candles)
            d_high = df["high"].iloc[-1]
            d_low = df["low"].iloc[-1]
            prev_high = df["high"].iloc[-2]
            prev_low = df["low"].iloc[-2]
            lines += [
                f"*{symbol.value}*",
                f"  Today H/L:  `{d_high:.2f}` / `{d_low:.2f}`",
                f"  Prev H/L:   `{prev_high:.2f}` / `{prev_low:.2f}`",
                "",
            ]
        except Exception as exc:
            lines.append(f"⚠️ {symbol.value}: {exc}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# Mode control (admin-only)
# ---------------------------------------------------------------------------

@admin_only
async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text(
            f"Current mode: `{settings.mode.value}`\\. Use /mode assist|paper|live",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    requested = args[0].lower()
    if requested not in ("assist", "paper", "live"):
        await update.message.reply_text("❌ Invalid mode\\. Choose: assist, paper, live", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if requested == "live":
        # Require PIN for live mode — use constant-time comparison to prevent timing attacks
        import secrets
        if len(args) < 2 or not secrets.compare_digest(args[1], settings.telegram_pin):
            await update.message.reply_text(
                "🔐 LIVE mode requires PIN: `/mode live <PIN>`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        await update.message.reply_text(
            "⚡ *LIVE mode enabled\\!*\n\n"
            "⚠️ Real money trades will be placed\\. "
            "Ensure cTrader credentials are configured\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    settings.mode = Mode(requested)
    await update.message.reply_text(
        f"✅ Mode switched to `{requested.upper()}`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    logger.info("Mode changed to {} by user {}", requested, update.effective_user.id)


@admin_only
async def cmd_live_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.args = ["live"] + list(context.args or [])
    await cmd_mode(update, context)


@admin_only
async def cmd_live_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings.mode = Mode.ASSIST
    await update.message.reply_text("✅ Switched to ASSIST mode \\(live trading OFF\\)", parse_mode=ParseMode.MARKDOWN_V2)


@admin_only
async def cmd_paper_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings.mode = Mode.PAPER
    await update.message.reply_text("✅ PAPER trading enabled", parse_mode=ParseMode.MARKDOWN_V2)


@admin_only
async def cmd_paper_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings.mode = Mode.ASSIST
    await update.message.reply_text("✅ Switched to ASSIST mode \\(paper trading OFF\\)", parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# Risk controls (admin-only)
# ---------------------------------------------------------------------------

@admin_only
async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) == 3 and args[0] == "set" and args[1] == "pct":
        try:
            pct = float(args[2])
            settings.risk_per_trade_pct = pct
            await update.message.reply_text(f"✅ Risk per trade set to {pct}%")
        except ValueError:
            await update.message.reply_text("❌ Invalid value")
        return

    text = (
        f"*⚙️ Risk Settings*\n\n"
        f"  Risk/trade:     {settings.risk_per_trade_pct}%\n"
        f"  Max trades/day: {settings.max_trades_per_day}\n"
        f"  Max losses/day: {settings.max_losses_per_day}\n"
        f"  Daily DD cap:   {settings.daily_dd_cap_pct}%\n"
        f"  SL multiplier:  {settings.sl_atr_multiplier}x ATR\n"
        f"  RR target:      {settings.tp_rr_ratio}:1\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


@admin_only
async def cmd_limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) >= 3 and args[0] == "set":
        key, val = args[1], args[2]
        try:
            if key == "max_losses":
                settings.max_losses_per_day = int(val)
                await update.message.reply_text(f"✅ max_losses_per_day = {val}")
            elif key == "daily_dd":
                settings.daily_dd_cap_pct = float(val)
                await update.message.reply_text(f"✅ daily_dd_cap = {val}%")
            else:
                await update.message.reply_text(f"❌ Unknown setting: {key}")
        except ValueError:
            await update.message.reply_text("❌ Invalid value")
        return

    state = await locks.get_state()
    text = (
        f"*📋 Daily Limits*\n\n"
        f"  Max trades/day:  {settings.max_trades_per_day}\n"
        f"  Max losses/day:  {settings.max_losses_per_day}\n"
        f"  Daily DD cap:    {settings.daily_dd_cap_pct}%\n"
        f"  Cooldown:        {settings.cooldown_min_after_loss}m after loss\n\n"
        f"  Today trades:    {state.trades_count}\n"
        f"  Today losses:    {state.losses_count}\n"
        f"  Today PnL:       {state.pnl:+.2f}\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


@admin_only
async def cmd_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text(
            f"Current cooldown setting: {settings.cooldown_min_after_loss}m\\. "
            "Use /cooldown <minutes>",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    try:
        minutes = int(args[0])
        settings.cooldown_min_after_loss = minutes
        await update.message.reply_text(f"✅ Cooldown set to {minutes} minutes")
    except ValueError:
        await update.message.reply_text("❌ Invalid value — provide minutes as integer")


# ---------------------------------------------------------------------------
# Safety / emergency commands (admin-only)
# ---------------------------------------------------------------------------

@admin_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    minutes = int(args[0]) if args else 60
    await locks.pause_trading(minutes)
    await update.message.reply_text(
        f"⏸ Trading paused for {minutes} minutes\\. Use /resume to cancel\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@admin_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await locks.resume_trading()
    await update.message.reply_text("▶️ Trading resumed\\.", parse_mode=ParseMode.MARKDOWN_V2)


@admin_only
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await locks.activate_kill_switch()
    await update.message.reply_text(
        "☠️ *KILL SWITCH ACTIVATED*\n\nAll trading halted\\. Use /unlock <PIN> to re\\-enable\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@admin_only
async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_pin(update, context):
        return
    await locks.deactivate_kill_switch()
    await locks.unlock_trading()
    await update.message.reply_text("🔓 All locks cleared\\. Trading re\\-enabled\\.", parse_mode=ParseMode.MARKDOWN_V2)
