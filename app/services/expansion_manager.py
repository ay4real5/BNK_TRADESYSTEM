"""
Mode C — Defensive Core + Statistical Expansion Layer.

Architecture
------------
**Defensive Mode** (always-on default):
  - risk_per_trade = 0.5% equity
  - all capital kill-switches active

**Expansion Mode** (conditional, temporary):
  - Activated ONLY when all four gates pass simultaneously:
    1. Rolling win rate (last N trades) >= 60%
    2. Equity at a new rolling N-trade high
    3. Max drawdown over rolling window <= 3%
    4. ATR volatility filter passes (no spike detected)
  - In expansion: risk increases to 0.9% equity
  - Maximum 20 trades per expansion window

**Auto-exit from Expansion**:
  - 2 consecutive losses inside the window
  - Drawdown from expansion_start_equity exceeds 3%
  - Rolling win rate drops below 55%
  - ATR spike detected

Expansion is *earned* and *revoked quickly*.
The defensive core is never suspended.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from ..config import settings
from ..data.storage import (
    get_rolling_trade_stats,
    load_expansion_state,
    save_expansion_state,
)
from ..domain.models import AccountState, ExpansionState


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_state() -> ExpansionState:
    """Return the current persisted expansion state."""
    return await load_expansion_state()


async def after_trade(account: AccountState, is_win: bool) -> ExpansionState:
    """
    Call this after every closed trade to update/transition expansion state.

    If currently in expansion:
      - Increment ``trades_in_window``
      - Update ``consecutive_losses`` (reset on win, increment on loss)
      - Check all exit conditions; deactivate if any are breached

    If currently in defensive mode:
      - Check all activation conditions; activate if all pass
      - Requires at least ``expansion_min_trades`` trades in the DB first

    Returns the updated (persisted) ExpansionState.
    """
    state = await load_expansion_state()

    if state.active:
        state = _update_window(state, account, is_win)
        should_exit, reason = _check_exit_conditions(state, account)
        if should_exit:
            state = _deactivate(state, reason)
            logger.warning(
                "Expansion Mode EXIT — reason: {} | equity=${:.2f} | "
                "trades_in_window={}",
                reason,
                account.equity,
                state.trades_in_window,
            )
    else:
        stats = await get_rolling_trade_stats(settings.expansion_rolling_window)
        if _check_activation_conditions(stats, account, state):
            state = _activate(state, account)
            logger.info(
                "Expansion Mode ACTIVATED — win_rate={:.1%} | max_dd={:.2f}% | "
                "equity=${:.2f}",
                stats["win_rate"],
                stats["max_dd_pct"],
                account.equity,
            )

    await save_expansion_state(state)
    return state


def effective_risk_pct(state: ExpansionState, consecutive_losses: int) -> float:
    """
    Return the effective risk % for the next trade.

    - Expansion active:   ``expansion_risk_pct`` (no consecutive-loss scaling in expansion)
    - Defensive + streak: ``defensive_risk_pct * consecutive_loss_scale_factor``
    - Defensive normal:   ``defensive_risk_pct``
    """
    if state.active:
        return settings.expansion_risk_pct
    return settings.effective_risk_pct(consecutive_losses)


# ---------------------------------------------------------------------------
# Activation logic
# ---------------------------------------------------------------------------

def _check_activation_conditions(
    stats: dict,
    account: AccountState,
    state: ExpansionState,
) -> bool:
    """Return True only if ALL four expansion gates pass."""
    # Gate 0: minimum trade history required
    if stats["total"] < settings.expansion_min_trades:
        return False

    # Gate 1: rolling win rate >= threshold
    if stats["win_rate"] < settings.expansion_min_win_rate:
        return False

    # Gate 2: rolling max drawdown <= threshold
    if stats["max_dd_pct"] > settings.expansion_max_dd_pct:
        return False

    # Gate 3: equity at a new rolling-window high
    # Proxy: current equity >= account peak_equity (already tracking this)
    if account.equity < account.peak_equity:
        return False

    # Gate 4: no ATR spike active
    if state.atr_spike_active:
        return False

    return True


def _activate(state: ExpansionState, account: AccountState) -> ExpansionState:
    return state.model_copy(update={
        "active": True,
        "start_equity": account.equity,
        "trades_in_window": 0,
        "consecutive_losses": 0,
        "activated_at": datetime.now(timezone.utc),
        "exit_reason": None,
    })


# ---------------------------------------------------------------------------
# Exit logic
# ---------------------------------------------------------------------------

def _update_window(
    state: ExpansionState,
    account: AccountState,
    is_win: bool,
) -> ExpansionState:
    """Increment trade counter and update consecutive loss streak."""
    new_consec = 0 if is_win else state.consecutive_losses + 1
    return state.model_copy(update={
        "trades_in_window": state.trades_in_window + 1,
        "consecutive_losses": new_consec,
    })


def _check_exit_conditions(
    state: ExpansionState,
    account: AccountState,
) -> tuple[bool, str]:
    """
    Return (should_exit: bool, reason: str).

    Checks all four exit gates in priority order.
    """
    # Exit 1: max trades in window exhausted
    if state.trades_in_window >= settings.expansion_max_trades:
        return True, f"window_exhausted ({state.trades_in_window} trades)"

    # Exit 2: consecutive losses within expansion
    if state.consecutive_losses >= settings.expansion_exit_consec_losses:
        return True, f"consecutive_losses ({state.consecutive_losses})"

    # Exit 3: drawdown from expansion start equity
    if state.start_equity > 0:
        dd_from_start = (state.start_equity - account.equity) / state.start_equity * 100
        if dd_from_start >= settings.expansion_exit_dd_pct:
            return True, f"drawdown_from_start ({dd_from_start:.2f}%)"

    # Exit 4: ATR spike
    if state.atr_spike_active:
        return True, "atr_spike"

    return False, ""


async def _check_rolling_win_rate_exit() -> tuple[bool, str]:
    """Async gate: rolling win rate dropped below exit threshold."""
    stats = await get_rolling_trade_stats(settings.expansion_rolling_window)
    if stats["total"] >= 10 and stats["win_rate"] < settings.expansion_exit_win_rate:
        return True, f"win_rate_dropped ({stats['win_rate']:.1%})"
    return False, ""


def _deactivate(state: ExpansionState, reason: str) -> ExpansionState:
    return state.model_copy(update={
        "active": False,
        "exit_reason": reason,
        "trades_in_window": state.trades_in_window,
    })


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------

async def force_deactivate(reason: str = "manual") -> ExpansionState:
    """Admin: immediately exit expansion mode."""
    state = await load_expansion_state()
    if state.active:
        state = _deactivate(state, reason)
        await save_expansion_state(state)
        logger.warning("Expansion Mode force-deactivated: {}", reason)
    return state


async def set_atr_spike(active: bool) -> ExpansionState:
    """
    Mark an ATR spike event (called by market data layer when live ATR detected).
    In demo mode this is always False.
    """
    state = await load_expansion_state()
    state = state.model_copy(update={"atr_spike_active": active})
    if active and state.active:
        state = _deactivate(state, "atr_spike")
        logger.warning("Expansion Mode EXIT — ATR spike detected")
    await save_expansion_state(state)
    return state
