"""
Message formatters — convert domain objects to human-readable Telegram messages.
All messages use Telegram MarkdownV2 (parse_mode='MarkdownV2') escaping.
"""

from __future__ import annotations

import re
from datetime import datetime

from ..config import settings
from ..domain.enums import Bias, Mode, Side, TradeOutcome
from ..domain.models import RiskState, TradeIdea, TradeResult


def _esc(text: str) -> str:
    """Escape special MarkdownV2 characters."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(special)}])", r"\\\1", str(text))


def _price(p: float, symbol_name: str = "") -> str:
    decimals = 2 if "XAU" in symbol_name else 4
    return f"{p:.{decimals}f}"


def format_signal(idea: TradeIdea) -> str:
    """Format a TradeIdea into a rich Telegram signal message."""
    sym = idea.symbol.value
    side_emoji = "🟢" if idea.side == Side.BUY else "🔴"
    bias_emoji = "📈" if idea.bias == Bias.BULLISH else "📉"
    mode_tag = f"[{idea.mode.value.upper()}]"

    score_bar = "🔥" * int(idea.score // 2) + "⬜" * (5 - int(idea.score // 2))

    lines = [
        f"*{_esc(mode_tag)} {side_emoji} {_esc(sym)} {_esc(idea.side.value.upper())} SIGNAL*",
        "",
        f"📊 Score: *{_esc(str(idea.score))}/10* {_esc(score_bar)}",
        f"{bias_emoji} Bias: {_esc(idea.bias.value.capitalize())}",
        "",
        f"💰 Entry:  `{_esc(_price(idea.entry, sym))}`",
        f"🛑 SL:     `{_esc(_price(idea.sl, sym))}`",
        f"🎯 TP:     `{_esc(_price(idea.tp, sym))}`",
        f"📐 RR:     *{_esc(str(idea.risk_reward))}:1*",
        "",
        "*Analysis:*",
    ]
    for r in idea.reasons:
        lines.append(f"  {_esc(r)}")

    lines += [
        "",
        f"🕐 {_esc(idea.ts.strftime('%Y\\-%m\\-%d %H:%M') + ' UTC')}",
    ]

    return "\n".join(lines)


def format_status(state: RiskState, last_scan: datetime | None = None) -> str:
    """Format the /status message."""
    lock_icon = "🔴" if state.is_locked else "🟢"
    kill_icon = "☠️ KILL SWITCH ON" if state.kill_switch else ""

    lines = [
        "*📊 Trading System Status*",
        "",
        f"⚙️ Mode:         `{_esc(settings.mode.value.upper())}`",
        f"{lock_icon} Trading:       {'LOCKED' if state.is_locked else 'ALLOWED'}",
    ]
    if kill_icon:
        lines.append(f"{_esc(kill_icon)}")
    if last_scan:
        lines.append(f"🕐 Last scan:    `{_esc(last_scan.strftime('%H:%M:%S'))} UTC`")
    lines += [
        "",
        "*📅 Today's Stats:*",
        f"  Trades:        {state.trades_count}/{settings.max_trades_per_day}",
        f"  Losses:        {state.losses_count}/{settings.max_losses_per_day}",
        f"  PnL:           {_esc(f'{state.pnl:+.2f}')}",
        f"  Drawdown:      {_esc(f'{state.drawdown_pct:.2f}')}%",
    ]
    if state.locked_until_ts and not state.kill_switch:
        remaining = max(0, int((state.locked_until_ts - datetime.utcnow()).total_seconds() // 60))
        reason = state.lock_reason.value if state.lock_reason else "locked"
        lines.append(f"  🔒 Lock reason: {_esc(reason)} \\({remaining}m\\)")

    return "\n".join(lines)


def format_daily_report(summary: dict) -> str:
    """Format the end-of-day summary report."""
    total = summary.get("total", 0)
    wins = summary.get("wins", 0)
    losses = summary.get("losses", 0)
    pnl = summary.get("total_pnl", 0.0)
    winrate = round(wins / total * 100, 1) if total > 0 else 0

    pnl_emoji = "✅" if pnl >= 0 else "❌"

    return "\n".join([
        "*📈 Daily Trading Report*",
        "",
        f"  Trades:   {total}",
        f"  Wins:     {wins}",
        f"  Losses:   {losses}",
        f"  Win Rate: {_esc(str(winrate))}%",
        f"  {pnl_emoji} Total PnL: {_esc(f'{pnl:+.2f}')}",
        "",
        f"🕐 Report time: {_esc(datetime.utcnow().strftime('%Y\\-%m\\-%d %H:%M'))} UTC",
    ])


def format_recent_signals(ideas: list[TradeIdea]) -> str:
    """Format a list of recent signals for /signals command."""
    if not ideas:
        return "📭 No recent signals found\\."

    lines = ["*📋 Recent Signals \\(last 10\\)*", ""]
    for i, idea in enumerate(ideas, 1):
        side_emoji = "🟢" if idea.side == Side.BUY else "🔴"
        lines.append(
            f"{i}\\. {side_emoji} *{_esc(idea.symbol.value)}* {_esc(idea.side.value.upper())} "
            f"@ `{_esc(_price(idea.entry, idea.symbol.value))}` — "
            f"Score: {_esc(str(idea.score))}/10 — {_esc(idea.status.value)}"
        )
    return "\n".join(lines)


HELP_TEXT = """\
*🤖 Gold/Silver Trading Assistant*

*Core Commands:*
/start — Show this help \\+ current mode
/help — List all commands
/status — System status, PnL, locks
/analyze — Force analysis scan now

*Analysis:*
/signals — Last 10 signals \\(scores \\+ reasons\\)
/bias — 1H bias for XAU/XAG
/levels — Key price levels

*Mode Control \\(admin\\):*
/mode assist — Analysis only mode
/mode paper — Paper trading
/mode live \\<PIN\\> — Enable live trading

*Risk Controls \\(admin\\):*
/risk — Show risk settings
/risk set pct \\<value\\> — Set risk %
/limits — Show daily limits
/limits set max\\_losses \\<n\\>
/limits set daily\\_dd \\<pct\\>
/cooldown \\<minutes\\> — Set cooldown

*Safety \\(admin\\):*
/pause \\<minutes\\> — Pause trading
/resume — Resume trading
/kill — EMERGENCY kill switch 🚨
/unlock \\<PIN\\> — Clear locks
"""
