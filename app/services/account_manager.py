"""
Account state manager.

Owns all mutation of the persistent AccountState row: applying closed-trade
PnL, recalculating drawdown, and updating the peak equity high-water mark.

Intraday tracking
-----------------
``equity_at_day_start`` is snapshotted once per calendar day (UTC).  On the
first trade of each day (or on server startup) the current equity is written
as ``equity_at_day_start`` so the intraday drawdown stop always measures from
*today's* opening equity rather than the all-time peak.

All public functions are async and safe to call concurrently (each uses a
fresh DB connection via the storage layer).
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from ..data.storage import load_account_state, save_account_state
from ..domain.models import AccountState


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def get_account() -> AccountState:
    """Return the current persistent account snapshot."""
    return await load_account_state()


async def apply_trade(pnl: float) -> AccountState:
    """
    Apply a closed-trade PnL to the account and persist the result.

    Updates:
    - ``balance``              += pnl
    - ``equity``                = balance  (demo: no open positions)
    - ``peak_equity``           = max(peak_equity, equity)
    - ``equity_at_day_start``   = equity snapshot at first trade of each UTC day
    - ``drawdown_pct``          = (peak_equity - equity) / peak_equity * 100
    - ``total_pnl``            += pnl
    - ``consecutive_losses``    increments on loss, resets to 0 on any win

    Returns the updated AccountState after saving.
    """
    current = await load_account_state()

    # Snapshot equity_at_day_start once per UTC calendar day
    last_date = current.last_updated.strftime("%Y-%m-%d") if current.last_updated else None
    if last_date != _today_utc():
        current = current.model_copy(update={"equity_at_day_start": current.equity})
        logger.info(
            "New trading day — equity_at_day_start snapshotted at ${:,.2f}",
            current.equity,
        )

    updated = current.apply_pnl(pnl)
    await save_account_state(updated)

    log_msg = (
        "Account updated: equity=${:.2f} | peak=${:.2f} | dd={:.2f}% | "
        "total_pnl={:+.2f} | consec_losses={}"
    )
    logger.debug(
        log_msg,
        updated.equity,
        updated.peak_equity,
        updated.drawdown_pct,
        updated.total_pnl,
        updated.consecutive_losses,
    )
    return updated


async def reset_account(starting_balance: float) -> AccountState:
    """
    Hard-reset the account to a fresh starting balance.
    Use for testing or after a full account reset event.
    """
    fresh = AccountState(
        starting_balance=starting_balance,
        balance=starting_balance,
        equity=starting_balance,
        peak_equity=starting_balance,
        equity_at_day_start=starting_balance,
    )
    await save_account_state(fresh)
    logger.warning("Account reset to starting balance ${:,.2f}", starting_balance)
    return fresh
