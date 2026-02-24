"""
Technical indicator features computed from OHLCV DataFrames.

All functions accept a pandas DataFrame (indexed by datetime, columns: open/high/low/close/volume)
and return the DataFrame with new columns appended.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from ..domain.errors import InsufficientDataError
from ..data.market_data import require_min_candles


def add_ema(df: pd.DataFrame, period: int, col: str = "close") -> pd.DataFrame:
    """Add EMA column: ema_{period}."""
    require_min_candles(df, period, f"EMA-{period}")
    df[f"ema_{period}"] = ta.ema(df[col], length=period)
    return df


def add_rsi(df: pd.DataFrame, period: int = 14, col: str = "close") -> pd.DataFrame:
    """Add RSI column: rsi_{period}."""
    require_min_candles(df, period + 1, f"RSI-{period}")
    df[f"rsi_{period}"] = ta.rsi(df[col], length=period)
    return df


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Add ATR column: atr_{period}."""
    require_min_candles(df, period + 1, f"ATR-{period}")
    df[f"atr_{period}"] = ta.atr(df["high"], df["low"], df["close"], length=period)
    return df


def add_bollinger(df: pd.DataFrame, period: int = 20, std: float = 2.0) -> pd.DataFrame:
    """Add Bollinger Band columns: bb_upper, bb_mid, bb_lower."""
    require_min_candles(df, period, f"BB-{period}")
    bbands = ta.bbands(df["close"], length=period, std=std)
    if bbands is not None:
        df["bb_upper"] = bbands[f"BBU_{period}_{std}"]
        df["bb_mid"] = bbands[f"BBM_{period}_{std}"]
        df["bb_lower"] = bbands[f"BBL_{period}_{std}"]
    return df


def add_all_features(
    df: pd.DataFrame,
    ema_bias_period: int = 200,
    ema_fast_period: int = 20,
    ema_slow_period: int = 50,
    rsi_period: int = 14,
    atr_period: int = 14,
) -> pd.DataFrame:
    """
    Apply the full feature set used by the strategy.
    Returns the DataFrame with all indicator columns added.
    """
    df = add_ema(df, ema_bias_period)
    df = add_ema(df, ema_fast_period)
    df = add_ema(df, ema_slow_period)
    df = add_rsi(df, rsi_period)
    df = add_atr(df, atr_period)
    return df


def get_swing_high(df: pd.DataFrame, lookback: int = 10) -> float:
    """Return the highest high over the last `lookback` candles."""
    return float(df["high"].tail(lookback).max())


def get_swing_low(df: pd.DataFrame, lookback: int = 10) -> float:
    """Return the lowest low over the last `lookback` candles."""
    return float(df["low"].tail(lookback).min())


def prev_candle_high(df: pd.DataFrame) -> float:
    """Return the high of the candle immediately before the last one."""
    if len(df) < 2:
        raise InsufficientDataError("Need at least 2 candles for prev_candle_high")
    return float(df["high"].iloc[-2])


def prev_candle_low(df: pd.DataFrame) -> float:
    """Return the low of the candle immediately before the last one."""
    if len(df) < 2:
        raise InsufficientDataError("Need at least 2 candles for prev_candle_low")
    return float(df["low"].iloc[-2])
