"""
Safeguards and pre-execution checks.

These run before any trade is submitted to the executor and act as
the last line of defence against bad orders.
"""

from __future__ import annotations

from loguru import logger

from ..config import settings
from ..domain.enums import Mode, Side, Symbol
from ..domain.errors import RiskViolation
from ..domain.models import TradeIdea


LIVE_CONFIRMATION_REQUIRED = True


def check_spread(spread: float, symbol: Symbol) -> None:
    """Raise RiskViolation if the spread is too wide."""
    max_spread = (
        settings.max_spread_xauusd
        if symbol == Symbol.XAUUSD
        else settings.max_spread_xagusd
    )
    if spread > max_spread:
        raise RiskViolation(
            f"Spread {spread:.4f} exceeds max {max_spread:.4f} for {symbol.value}"
        )


def check_volatility(volatility_regime: str) -> None:
    """Raise RiskViolation if volatility is extreme."""
    if volatility_regime == "extreme":
        raise RiskViolation("Extreme volatility detected — trade blocked by safeguard")


def check_sl_tp_valid(idea: TradeIdea) -> None:
    """Raise RiskViolation if SL/TP geometry is wrong."""
    if idea.side == Side.BUY:
        if idea.sl >= idea.entry:
            raise RiskViolation(f"BUY SL ({idea.sl}) must be below entry ({idea.entry})")
        if idea.tp <= idea.entry:
            raise RiskViolation(f"BUY TP ({idea.tp}) must be above entry ({idea.entry})")
    else:
        if idea.sl <= idea.entry:
            raise RiskViolation(f"SELL SL ({idea.sl}) must be above entry ({idea.entry})")
        if idea.tp >= idea.entry:
            raise RiskViolation(f"SELL TP ({idea.tp}) must be below entry ({idea.entry})")


def check_min_rr(idea: TradeIdea, min_rr: float = 1.5) -> None:
    """Raise RiskViolation if RR is below minimum."""
    if idea.risk_reward < min_rr:
        raise RiskViolation(
            f"RR {idea.risk_reward:.2f} below minimum {min_rr:.2f}"
        )


def check_live_mode_requirements(idea: TradeIdea) -> None:
    """
    Extra checks for LIVE mode only.
    In LIVE mode we require a higher minimum score.
    """
    if idea.mode != Mode.LIVE:
        return
    if idea.score < 6.0:
        raise RiskViolation(
            f"LIVE mode requires score ≥ 6.0, got {idea.score}"
        )


def run_all_safeguards(
    idea: TradeIdea,
    spread: float,
    volatility_regime: str,
) -> None:
    """
    Run all pre-execution safeguards.
    Raises RiskViolation on the first failure.
    """
    check_spread(spread, idea.symbol)
    check_volatility(volatility_regime)
    check_sl_tp_valid(idea)
    check_min_rr(idea)
    check_live_mode_requirements(idea)
    logger.debug("All safeguards passed for {} {} @ {}", idea.symbol, idea.side, idea.entry)
