"""
SL/TP calculation and position sizing helpers.

Position size is expressed in lots for cTrader (1 lot = 100 oz for XAU).
For paper trading we default to a notional size of 1 unit.
"""

from __future__ import annotations

import math

import pandas as pd

from ..config import settings
from ..domain.enums import Side, Symbol


# ---------------------------------------------------------------------------
# SL / TP
# ---------------------------------------------------------------------------

def calc_sl_tp(
    side: Side,
    entry: float,
    atr: float,
    sl_multiplier: float | None = None,
    rr_ratio: float | None = None,
) -> tuple[float, float]:
    """
    Calculate stop-loss and take-profit from ATR.

    Returns (sl, tp) prices.
    """
    if sl_multiplier is None:
        sl_multiplier = settings.sl_atr_multiplier
    if rr_ratio is None:
        rr_ratio = settings.tp_rr_ratio

    risk = atr * sl_multiplier

    if side == Side.BUY:
        sl = entry - risk
        tp = entry + risk * rr_ratio
    else:
        sl = entry + risk
        tp = entry - risk * rr_ratio

    return round(sl, 5), round(tp, 5)


def sl_from_swing(
    side: Side,
    entry: float,
    swing_high: float,
    swing_low: float,
    buffer_pct: float = 0.001,
) -> float:
    """
    Calculate SL from the nearest swing high/low with a small buffer.

    Used as an alternative to ATR-based SL.
    """
    if side == Side.BUY:
        sl = swing_low * (1 - buffer_pct)
    else:
        sl = swing_high * (1 + buffer_pct)
    return round(sl, 5)


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def calc_position_size(
    account_balance: float,
    entry: float,
    sl: float,
    symbol: Symbol,
    risk_pct: float | None = None,
) -> float:
    """
    Return position size in lots.

    Formula:
        risk_amount = account_balance * risk_pct / 100
        pip_value   = pip_size * lot_size
        lots        = risk_amount / (sl_distance / pip_size * pip_value)

    For XAUUSD: 1 lot = 100 oz, pip = $0.01
    For XAGUSD: 1 lot = 5000 oz, pip = $0.001
    """
    if risk_pct is None:
        risk_pct = settings.risk_per_trade_pct

    risk_amount = account_balance * (risk_pct / 100)
    sl_distance = abs(entry - sl)

    if sl_distance == 0:
        return 0.0

    # Pip sizes and lot sizes per symbol
    pip_data = {
        Symbol.XAUUSD: {"pip_size": 0.01, "lot_size": 100},
        Symbol.XAGUSD: {"pip_size": 0.001, "lot_size": 5000},
    }
    info = pip_data.get(symbol, {"pip_size": 0.01, "lot_size": 100})

    # Dollar value per pip per lot
    pip_value_per_lot = info["pip_size"] * info["lot_size"]

    # SL distance in pips
    sl_in_pips = sl_distance / info["pip_size"]

    # Lots = risk_amount / (sl_pips * pip_value_per_lot)
    lots = risk_amount / (sl_in_pips * pip_value_per_lot)

    # Round down to 2 decimal places (standard lot precision)
    return math.floor(lots * 100) / 100
