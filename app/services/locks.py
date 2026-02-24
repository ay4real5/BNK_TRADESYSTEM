"""
Risk governor and trading locks.

This module implements all daily risk controls:
  - Max trades per day
  - Max losses per day
  - Daily drawdown cap
  - Cooldown after a loss
  - Manual pause / kill switch
"""

from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger

from ..config import settings
from ..domain.enums import LockReason
from ..domain.errors import LockError
from ..domain.models import RiskState
from ..data.storage import load_risk_state, save_risk_state


async def get_state() -> RiskState:
    return await load_risk_state()


async def check_can_trade(state: RiskState | None = None) -> RiskState:
    """
    Verify that trading is permitted under current risk state.
    Raises LockError if blocked, otherwise returns the current state.
    """
    if state is None:
        state = await get_state()

    # Kill switch
    if state.kill_switch:
        raise LockError(LockReason.KILL_SWITCH.value)

    # Manual pause
    if state.paused_until_ts and datetime.utcnow() < state.paused_until_ts:
        remaining = int((state.paused_until_ts - datetime.utcnow()).total_seconds() // 60)
        raise LockError(f"{LockReason.PAUSED.value} — {remaining}m remaining")

    # Cooldown / daily lock
    if state.locked_until_ts and datetime.utcnow() < state.locked_until_ts:
        remaining = int((state.locked_until_ts - datetime.utcnow()).total_seconds() // 60)
        reason = state.lock_reason.value if state.lock_reason else LockReason.COOLDOWN.value
        raise LockError(f"{reason} — {remaining}m remaining")

    # Max trades per day
    if state.trades_count >= settings.max_trades_per_day:
        raise LockError(
            f"{LockReason.MAX_TRADES.value} — {state.trades_count}/{settings.max_trades_per_day} used"
        )

    # Max losses per day
    if state.losses_count >= settings.max_losses_per_day:
        raise LockError(
            f"{LockReason.MAX_LOSSES.value} — {state.losses_count}/{settings.max_losses_per_day} losses"
        )

    # Daily drawdown cap
    if state.drawdown_pct >= settings.daily_dd_cap_pct:
        raise LockError(
            f"{LockReason.DAILY_DD.value} — {state.drawdown_pct:.2f}% >= {settings.daily_dd_cap_pct}%"
        )

    return state


async def record_trade(pnl: float, is_loss: bool) -> RiskState:
    """
    Update daily state after a trade closes.
    Applies cooldown if the trade was a loss.
    """
    state = await get_state()
    state.trades_count += 1

    if is_loss:
        state.losses_count += 1
        state.pnl += pnl
        # Apply cooldown
        cooldown_end = datetime.utcnow() + timedelta(minutes=settings.cooldown_min_after_loss)
        state.locked_until_ts = cooldown_end
        state.lock_reason = LockReason.COOLDOWN
        logger.warning(
            "Loss recorded — cooldown active until {} UTC",
            cooldown_end.strftime("%H:%M"),
        )
    else:
        state.pnl += pnl

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
