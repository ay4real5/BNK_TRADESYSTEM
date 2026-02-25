"""
Dynamic risk calculator — equity-based, capital-preservation first.

Design principles
-----------------
* **All limits scale with live equity** — limits tighten as account shrinks,
  relax as equity grows to new highs.
* **Three independent stop layers**:
  1. *Daily loss limit* — 2 % of current equity (or hard abs floor if set)
  2. *Intraday drawdown stop* — 5 % from today's opening equity; hard stop
     for the session regardless of how the day started
  3. *Total drawdown kill-switch* — 10 % from peak equity; permanent halt

* **Consecutive-loss position scaling** — after 3 consecutive losses, risk
  per trade is automatically halved (0.5 % → 0.25 %).  Resets on any win.

Convention: all returned loss-limit values are **negative numbers**.
e.g. -200.0 means "halt if today's PnL reaches -$200".
"""

from __future__ import annotations

from ..config import settings


# ---------------------------------------------------------------------------
# Daily loss limit  (equity-based)
# ---------------------------------------------------------------------------

def compute_daily_loss_limit(equity: float | None = None) -> float:
    """
    Return the maximum permitted daily drawdown as a negative USD amount.

    Uses current equity so the limit automatically scales with account growth
    and drawdown.  If ``max_daily_loss_abs`` is set, the stricter (less-negative)
    of the two limits applies.

    Returns:
        A negative float, e.g. -200.0 for $10 000 equity at 2 %.
    """
    if equity is None:
        equity = settings.account_balance

    pct_limit = -(equity * settings.max_daily_loss_pct / 100.0)

    if settings.max_daily_loss_abs is not None:
        # max() → less-negative → stricter (triggers sooner, protects more).
        # Example: max(-200, -150) = -150  → only $150 of daily loss allowed.
        return max(pct_limit, settings.max_daily_loss_abs)

    return pct_limit


def is_daily_loss_breached(pnl_today: float, equity: float | None = None) -> bool:
    """Return True if today's cumulative PnL has hit or passed the daily limit."""
    return pnl_today <= compute_daily_loss_limit(equity)


# ---------------------------------------------------------------------------
# Intraday drawdown hard stop  (equity_at_day_start-based)
# ---------------------------------------------------------------------------

def compute_intraday_dd_limit(equity_at_day_start: float | None = None) -> float:
    """
    Return the maximum intraday drawdown from today's opening equity.

    This is a hard stop: once today's equity loss from open exceeds
    ``intraday_dd_stop_pct`` (default 5 %), trading halts for the rest of
    the session regardless of cumulative daily PnL.

    Example: started day at $10 000 → limit = -$500 (5 %).  Even if the
    account previously had $9 500 equity (so daily loss limit is only -$190),
    this layer catches a fast intraday move down.

    Returns:
        A negative float, e.g. -500.0 for $10 000 day-open equity at 5 %.
    """
    if equity_at_day_start is None:
        equity_at_day_start = settings.account_balance
    return -(equity_at_day_start * settings.intraday_dd_stop_pct / 100.0)


def is_intraday_dd_breached(
    equity: float,
    equity_at_day_start: float | None = None,
) -> bool:
    """
    Return True if today's intraday drawdown has hit or exceeded the hard stop.

    Args:
        equity:               Current live equity.
        equity_at_day_start:  Equity at the open of the current trading day.
    """
    if equity_at_day_start is None:
        equity_at_day_start = settings.account_balance
    intraday_pnl = equity - equity_at_day_start   # negative when below open
    return intraday_pnl <= compute_intraday_dd_limit(equity_at_day_start)


def intraday_dd_limit_str(equity_at_day_start: float | None = None) -> str:
    """Return a human-readable description of the intraday DD stop."""
    start = equity_at_day_start if equity_at_day_start is not None else settings.account_balance
    limit = compute_intraday_dd_limit(start)
    pct = settings.intraday_dd_stop_pct
    return f"${limit:.2f} ({pct}% of day-open ${start:,.2f})"


# ---------------------------------------------------------------------------
# Total drawdown limit  (peak-equity-based)
# ---------------------------------------------------------------------------

def compute_max_drawdown_limit(peak_equity: float | None = None) -> float:
    """
    Return the maximum tolerated drawdown as a negative USD amount from peak.

    e.g. peak=$10 000, max_total_drawdown_pct=10% → limit=-$1 000.
    """
    if peak_equity is None:
        peak_equity = settings.account_balance
    return -(peak_equity * settings.max_total_drawdown_pct / 100.0)


def is_total_drawdown_breached(equity: float, peak_equity: float | None = None) -> bool:
    """
    Return True if the current equity has fallen too far below peak equity.
    """
    if peak_equity is None:
        peak_equity = settings.account_balance
    current_drawdown = equity - peak_equity   # negative when below peak
    return current_drawdown <= compute_max_drawdown_limit(peak_equity)


# ---------------------------------------------------------------------------
# Position sizing  (equity-based + consecutive-loss scaling)
# ---------------------------------------------------------------------------

def compute_position_pnl(
    equity: float | None = None,
    consecutive_losses: int = 0,
    expansion_active: bool = False,
) -> dict[str, float]:
    """
    Return indicative WIN/LOSS PnL amounts for a demo paper trade.

    Sizing logic (Mode C):
    - **Expansion active**: use ``expansion_risk_pct`` (default 0.9%)
    - **Defensive + losing streak**: halved risk via consecutive-loss scaling
    - **Defensive normal**: full ``defensive_risk_pct`` (default 0.5%)

    Args:
        equity:            Current account equity.
        consecutive_losses: Consecutive loss streak (ignored in expansion mode).
        expansion_active:  True when Mode C expansion window is open.

    Returns:
        {"win": ..., "loss": ..., "risk_pct": ..., "mode": "expansion"|"defensive"}
    """
    if equity is None:
        equity = settings.account_balance

    if expansion_active:
        effective_pct = settings.expansion_risk_pct
        mode_label = "expansion"
    else:
        effective_pct = settings.effective_risk_pct(consecutive_losses)
        mode_label = "defensive"

    risk_amount = equity * (effective_pct / 100.0)
    win_amount = round(risk_amount * settings.tp_rr_ratio, 2)
    loss_amount = round(-risk_amount, 2)

    return {
        "win": win_amount,
        "loss": loss_amount,
        "risk_pct": effective_pct,
        "mode": mode_label,
    }


# ---------------------------------------------------------------------------
# Human-readable summaries
# ---------------------------------------------------------------------------

def daily_loss_limit_str(equity: float | None = None) -> str:
    """Return a human-readable description of the current daily loss limit."""
    limit = compute_daily_loss_limit(equity)
    eq = equity if equity is not None else settings.account_balance
    pct = settings.max_daily_loss_pct
    if settings.max_daily_loss_abs is not None and settings.max_daily_loss_abs > limit:
        return (
            f"${limit:.2f} (stricter of {pct}% of equity ${eq:,.2f} "
            f"or hard floor ${settings.max_daily_loss_abs:.2f})"
        )
    return f"${limit:.2f} ({pct}% of equity ${eq:,.2f})"


def total_drawdown_limit_str(peak_equity: float | None = None) -> str:
    """Return a human-readable description of the total drawdown kill-switch."""
    peak = peak_equity if peak_equity is not None else settings.account_balance
    limit = compute_max_drawdown_limit(peak)
    pct = settings.max_total_drawdown_pct
    return f"${limit:.2f} ({pct}% of peak ${peak:,.2f})"
