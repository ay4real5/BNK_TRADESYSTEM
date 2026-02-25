"""
Risk governor and trading locks.

This module implements all daily risk controls:
  - Kill switch / manual pause
  - Session gate (London / NY windows only)
  - News blackout gate
  - Max trades per day
  - Max losses per day
  - Daily drawdown cap
  - Cooldown after a loss
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger

from ..config import settings
from ..domain.enums import LockReason
from ..domain.errors import LockError
from ..domain.models import RiskState
from ..data.storage import load_risk_state, save_risk_state
from . import account_manager, risk_manager


async def _log_session_block(detail: str) -> None:
    """Write a session_block entry to the execution_events audit log."""
    try:
        from ..data.storage import log_execution_event
        await log_execution_event(
            event_type="session_block",
            symbol=None,
            detail=detail,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("Could not log session_block: {}", exc)


async def get_state() -> RiskState:
    return await load_risk_state()


async def check_can_trade(state: RiskState | None = None) -> RiskState:
    """
    Verify that trading is permitted under current risk state.
    Raises LockError if blocked, otherwise returns the current state.

    Gate order (fastest / cheapest first):
      1. Kill switch
      2. Manual pause
      3. Session gate   (UTC hour check)
      4. News blackout  (file check)
      5. Cooldown / daily lock (DB timestamp)
      6. Daily counters (max trades / losses)
      7. Open position count
      8. Drawdown gates (account equity)
    """
    if state is None:
        state = await get_state()

    # ── 1. Kill switch ────────────────────────────────────────────────────
    if state.kill_switch:
        raise LockError(LockReason.KILL_SWITCH.value)

    # ── 2. Manual pause ───────────────────────────────────────────────────
    if state.paused_until_ts and datetime.utcnow() < state.paused_until_ts:
        remaining = int((state.paused_until_ts - datetime.utcnow()).total_seconds() // 60)
        raise LockError(f"{LockReason.PAUSED.value} — {remaining}m remaining")

    # ── 3. Session gate ───────────────────────────────────────────────────
    if settings.session_gate_enabled:
        hour = datetime.now(timezone.utc).hour
        in_london = settings.london_open_utc <= hour < settings.london_close_utc
        in_ny     = settings.ny_open_utc     <= hour < settings.ny_close_utc
        if not (in_london or in_ny):
            detail = (
                f"UTC hour={hour} outside London ({settings.london_open_utc:02d}:00–"
                f"{settings.london_close_utc:02d}:00) and "
                f"NY ({settings.ny_open_utc:02d}:00–{settings.ny_close_utc:02d}:00)"
            )
            await _log_session_block(detail)
            raise LockError(f"{LockReason.OUT_OF_SESSION.value} — {detail}")

    # ── 4. News blackout ──────────────────────────────────────────────────
    from .news_filter import is_news_window
    blocked, news_reason = await is_news_window()
    if blocked:
        raise LockError(f"{LockReason.NEWS_FILTER.value} — {news_reason}")

    # ── 5. Cooldown / daily lock ──────────────────────────────────────────
    if state.locked_until_ts and datetime.utcnow() < state.locked_until_ts:
        remaining = int((state.locked_until_ts - datetime.utcnow()).total_seconds() // 60)
        reason = state.lock_reason.value if state.lock_reason else LockReason.COOLDOWN.value
        raise LockError(f"{reason} — {remaining}m remaining")

    # ── 6a. Max trades per day ────────────────────────────────────────────
    if state.trades_count >= settings.max_trades_per_day:
        raise LockError(
            f"{LockReason.MAX_TRADES.value} — {state.trades_count}/{settings.max_trades_per_day} used"
        )

    # ── 6b. Max losses per day ────────────────────────────────────────────
    if state.losses_count >= settings.max_losses_per_day:
        raise LockError(
            f"{LockReason.MAX_LOSSES.value} — {state.losses_count}/{settings.max_losses_per_day} losses"
        )

    # ── 7. Max simultaneous open positions ────────────────────────────────
    from ..data import storage
    from ..domain.enums import Mode as _Mode
    open_trades = await storage.get_open_trades()
    live_open = [t for t in open_trades if t.mode in (_Mode.DEMO, _Mode.LIVE)]
    if len(live_open) >= settings.max_open_positions:
        raise LockError(
            f"max_open_positions — {len(live_open)}/{settings.max_open_positions} positions open"
        )

    # ── 8. Daily drawdown cap ─────────────────────────────────────────────
    if state.drawdown_pct >= settings.daily_dd_cap_pct:
        raise LockError(
            f"{LockReason.DAILY_DD.value} — {state.drawdown_pct:.2f}% >= {settings.daily_dd_cap_pct}%"
        )

    # ── 8b. Equity-based checks (require DB read) ─────────────────────────
    account = await account_manager.get_account()

    if risk_manager.is_total_drawdown_breached(account.equity, account.peak_equity):
        raise LockError(
            f"{LockReason.TOTAL_DRAWDOWN.value} — "
            f"equity ${account.equity:,.2f} | "
            f"{risk_manager.total_drawdown_limit_str(account.peak_equity)}"
        )

    if risk_manager.is_intraday_dd_breached(account.equity, account.equity_at_day_start):
        raise LockError(
            f"{LockReason.INTRADAY_DD_STOP.value} — "
            f"equity ${account.equity:,.2f} | "
            f"{risk_manager.intraday_dd_limit_str(account.equity_at_day_start)}"
        )

    if risk_manager.is_daily_loss_breached(state.pnl, account.equity):
        raise LockError(
            f"{LockReason.DAILY_LOSS_LIMIT.value} — "
            f"pnl ${state.pnl:+.2f} reached limit {risk_manager.daily_loss_limit_str(account.equity)}"
        )

    return state


async def record_trade(pnl: float, is_loss: bool) -> RiskState:
    """
    Update daily state after a trade closes.
    Applies a cooldown for any loss; locks for the rest of the day if the
    dynamic daily loss limit or intraday drawdown stop is breached.
    """
    state = await get_state()
    state.trades_count += 1

    if is_loss:
        state.losses_count += 1

    state.pnl = round(state.pnl + pnl, 2)

    # Load live equity for limit calculations
    account = await account_manager.get_account()

    # Check if the daily loss limit has now been breached
    if risk_manager.is_daily_loss_breached(state.pnl, account.equity):
        now = datetime.utcnow()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        state.locked_until_ts = midnight
        state.lock_reason = LockReason.DAILY_LOSS_LIMIT
        logger.critical(
            "Daily loss limit breached — pnl ${:+.2f} / limit {} — trading locked until {} UTC",
            state.pnl,
            risk_manager.daily_loss_limit_str(account.equity),
            midnight.strftime("%H:%M"),
        )
    elif risk_manager.is_intraday_dd_breached(account.equity, account.equity_at_day_start):
        # Intraday DD hard stop — lock until midnight UTC
        now = datetime.utcnow()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        state.locked_until_ts = midnight
        state.lock_reason = LockReason.INTRADAY_DD_STOP
        logger.critical(
            "INTRADAY DD STOP — equity ${:.2f} dropped >{}% from day-open ${:.2f} — locked until {} UTC",
            account.equity,
            settings.intraday_dd_stop_pct,
            account.equity_at_day_start,
            midnight.strftime("%H:%M"),
        )
    elif risk_manager.is_total_drawdown_breached(account.equity, account.peak_equity):
        # Total drawdown kill-switch — permanent lock, requires manual reset
        state.kill_switch = True
        state.lock_reason = LockReason.TOTAL_DRAWDOWN
        logger.critical(
            "TOTAL DRAWDOWN EXCEEDED — equity ${:.2f} / {} — kill switch engaged",
            account.equity,
            risk_manager.total_drawdown_limit_str(account.peak_equity),
        )
    elif is_loss:
        # Normal per-loss cooldown
        cooldown_end = datetime.utcnow() + timedelta(minutes=settings.cooldown_min_after_loss)
        state.locked_until_ts = cooldown_end
        state.lock_reason = LockReason.COOLDOWN
        logger.warning(
            "Loss recorded — cooldown active until {} UTC",
            cooldown_end.strftime("%H:%M"),
        )

    await save_risk_state(state)
    return state


async def activate_kill_switch() -> RiskState:
    """Immediately halt all trading."""
    state = await get_state()
    state.kill_switch = True
    await save_risk_state(state)
    logger.critical("KILL SWITCH activated — all trading halted")
    return state


async def deactivate_kill_switch() -> RiskState:
    """Re-enable trading after kill switch."""
    state = await get_state()
    state.kill_switch = False
    await save_risk_state(state)
    logger.info("Kill switch deactivated")
    return state


async def pause_trading(minutes: int) -> RiskState:
    """Pause trading for `minutes` minutes."""
    state = await get_state()
    state.paused_until_ts = datetime.utcnow() + timedelta(minutes=minutes)
    await save_risk_state(state)
    logger.info("Trading paused for {} minutes", minutes)
    return state


async def resume_trading() -> RiskState:
    """Remove manual pause."""
    state = await get_state()
    state.paused_until_ts = None
    await save_risk_state(state)
    logger.info("Trading resumed")
    return state


async def unlock_trading() -> RiskState:
    """Admin unlock — clear all cooldowns/locks (not kill switch)."""
    state = await get_state()
    state.locked_until_ts = None
    state.lock_reason = None
    state.paused_until_ts = None
    await save_risk_state(state)
    logger.info("Trading locks cleared by admin")
    return state


async def set_cooldown(minutes: int) -> RiskState:
    """Override cooldown and apply immediately."""
    state = await get_state()
    state.locked_until_ts = datetime.utcnow() + timedelta(minutes=minutes)
    state.lock_reason = LockReason.COOLDOWN
    await save_risk_state(state)
    logger.info("Cooldown set for {} minutes", minutes)
    return state
