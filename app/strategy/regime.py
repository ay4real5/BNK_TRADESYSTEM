"""
Volatility / trend regime detection.

The regime influences position sizing, filter thresholds, and whether
the strategy should be active at all.
"""

from __future__ import annotations

import pandas as pd

from ..domain.models import MarketContext


def classify_volatility(
    df: pd.DataFrame,
    atr_period: int = 14,
    spike_multiplier: float = 2.5,
    low_multiplier: float = 0.5,
) -> str:
    """
    Classify current volatility as: 'low' | 'normal' | 'high' | 'extreme'.

    Compares the current ATR to its rolling mean over the last `atr_period * 3` bars.
    """
    col = f"atr_{atr_period}"
    if col not in df.columns or df[col].isna().all():
        return "normal"

    current_atr = df[col].iloc[-1]
    avg_atr = df[col].tail(atr_period * 3).mean()

    if avg_atr == 0:
        return "normal"

    ratio = current_atr / avg_atr

    if ratio >= spike_multiplier:
        return "extreme"
    elif ratio >= 1.5:
        return "high"
    elif ratio <= low_multiplier:
        return "low"
    return "normal"


def is_trending(df: pd.DataFrame, ema_fast: int = 20, ema_slow: int = 50) -> bool:
    """Return True if EMAs are fanned out (trend mode vs. chop)."""
    fast_col = f"ema_{ema_fast}"
    slow_col = f"ema_{ema_slow}"
    if fast_col not in df.columns or slow_col not in df.columns:
        return False
    last_fast = df[fast_col].iloc[-1]
    last_slow = df[slow_col].iloc[-1]
    # Consider trending if separation > 0.1% of price
    price = df["close"].iloc[-1]
    separation = abs(last_fast - last_slow) / price
    return separation > 0.001


def update_context_regime(ctx: MarketContext, df_15m: pd.DataFrame) -> MarketContext:
    """Mutate a MarketContext with the current volatility regime from 15m data."""
    ctx.volatility_regime = classify_volatility(df_15m)
    return ctx
