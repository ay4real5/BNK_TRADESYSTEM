"""
Core strategy rules for Gold/Silver trading.

Timeframes:
  - Bias determination: 1H (EMA200)
  - Entry triggers:     15m (EMA20/50 pullback + RSI cross + candle close)

Rule logic:
  1. Determine bias from 1H EMA200
  2. On 15m:
     a. Price must be near EMA20 or EMA50 (pullback)
     b. RSI must cross above 50 (buy) / below 50 (sell)
     c. Latest candle must break prior candle's high (buy) or low (sell)
  3. Apply filters (spread, volatility, session, news)
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from loguru import logger

from ..config import settings
from ..domain.enums import Bias, Mode, Side, Symbol
from ..domain.models import Candle, MarketContext, TradeIdea
from ..strategy.features import (
    add_all_features,
    get_swing_high,
    get_swing_low,
    prev_candle_high,
    prev_candle_low,
)
from ..strategy.regime import classify_volatility
from ..strategy.risk import calc_sl_tp
from ..strategy.scorer import score_setup
from ..data.market_data import candles_to_df


def determine_bias(df_1h: pd.DataFrame) -> Bias:
    """
    Determine 1H directional bias using EMA200.

    Bullish  → close > EMA200
    Bearish  → close < EMA200
    Neutral  → EMA200 not available yet
    """
    col = f"ema_{settings.ema_bias_period}"
    if col not in df_1h.columns:
        return Bias.NEUTRAL
    last_close = df_1h["close"].iloc[-1]
    last_ema = df_1h[col].iloc[-1]
    if pd.isna(last_ema):
        return Bias.NEUTRAL
    return Bias.BULLISH if last_close > last_ema else Bias.BEARISH


def _is_near_ema(price: float, ema: float, tolerance_pct: float = 0.003) -> bool:
    """Return True if price is within tolerance_pct of the EMA."""
    if ema == 0:
        return False
    return abs(price - ema) / ema <= tolerance_pct


def _rsi_cross(df: pd.DataFrame, rsi_col: str, side: Side, lookback: int = 3) -> bool:
    """
    Check if RSI recently crossed the 50 level in the direction of the trade.

    For a BUY we want RSI to have crossed above 50 in the last `lookback` bars.
    For a SELL we want RSI to have crossed below 50 in the last `lookback` bars.
    """
    if rsi_col not in df.columns or len(df) < lookback + 1:
        return False
    recent = df[rsi_col].tail(lookback + 1).values
    if side == Side.BUY:
        # At some point in recent history RSI was below 50, and it's now above
        return recent[-1] > 50 and any(v < 50 for v in recent[:-1])
    else:
        return recent[-1] < 50 and any(v > 50 for v in recent[:-1])


def _candle_breakout(df: pd.DataFrame, side: Side) -> bool:
    """
    Return True if the last closed candle broke the prior candle's high (buy)
    or low (sell).
    """
    if len(df) < 2:
        return False
    last_close = df["close"].iloc[-1]
    if side == Side.BUY:
        return last_close > prev_candle_high(df)
    else:
        return last_close < prev_candle_low(df)


def is_session_active(ts: datetime) -> tuple[bool, bool]:
    """
    Return (is_london, is_ny) based on UTC hour.
    """
    hour = ts.hour
    is_london = settings.london_open_utc <= hour < settings.london_close_utc
    is_ny = settings.ny_open_utc <= hour < settings.ny_close_utc
    return is_london, is_ny


def evaluate_strategy(
    symbol: Symbol,
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    mode: Mode,
) -> TradeIdea | None:
    """
    Run the full strategy rule-set and return a TradeIdea if a valid
    setup is found, otherwise None.
    """
    if len(candles_15m) < settings.ema_bias_period or len(candles_1h) < settings.ema_bias_period:
        logger.debug("{}: insufficient candles for strategy evaluation", symbol.value)
        return None

    df_1h = candles_to_df(candles_1h)
    df_15m = candles_to_df(candles_15m)

    # Add features to both timeframes
    df_1h = add_all_features(
        df_1h,
        ema_bias_period=settings.ema_bias_period,
        ema_fast_period=settings.ema_fast_period,
        ema_slow_period=settings.ema_slow_period,
        rsi_period=settings.rsi_period,
        atr_period=settings.atr_period,
    )
    df_15m = add_all_features(
        df_15m,
        ema_bias_period=settings.ema_bias_period,
        ema_fast_period=settings.ema_fast_period,
        ema_slow_period=settings.ema_slow_period,
        rsi_period=settings.rsi_period,
        atr_period=settings.atr_period,
    )

    bias = determine_bias(df_1h)
    if bias == Bias.NEUTRAL:
        return None

    side = Side.BUY if bias == Bias.BULLISH else Side.SELL

    # Current 15m values
    last = df_15m.iloc[-1]
    price = float(last["close"])
    ema20 = float(last.get(f"ema_{settings.ema_fast_period}", 0) or 0)
    ema50 = float(last.get(f"ema_{settings.ema_slow_period}", 0) or 0)
    rsi_val = float(last.get(f"rsi_{settings.rsi_period}", 50) or 50)
    atr_val = float(last.get(f"atr_{settings.atr_period}", 0) or 0)
    rsi_col = f"rsi_{settings.rsi_period}"

    # --- Rule 1: Price near EMA20 or EMA50 (pullback) ---
    near_ema = _is_near_ema(price, ema20) or _is_near_ema(price, ema50)
    if not near_ema:
        return None

    # --- Rule 2: RSI cross ---
    if not _rsi_cross(df_15m, rsi_col, side):
        return None

    # --- Rule 3: Candle breakout trigger ---
    if not _candle_breakout(df_15m, side):
        return None

    if atr_val == 0:
        return None

    entry = price
    sl, tp = calc_sl_tp(side, entry, atr_val)

    ts = df_15m.index[-1]
    is_london, is_ny = is_session_active(ts if isinstance(ts, datetime) else ts.to_pydatetime())

    volatility_regime = classify_volatility(df_15m)

    ctx = MarketContext(
        symbol=symbol,
        ts=ts if isinstance(ts, datetime) else ts.to_pydatetime(),
        bias=bias,
        price=price,
        spread=0.0,  # will be updated by caller
        atr_15m=atr_val,
        ema200_1h=float(df_1h[f"ema_{settings.ema_bias_period}"].iloc[-1] or 0),
        ema20_15m=ema20,
        ema50_15m=ema50,
        rsi_15m=rsi_val,
        is_london_session=is_london,
        is_ny_session=is_ny,
        volatility_regime=volatility_regime,
    )

    idea = TradeIdea(
        symbol=symbol,
        side=side,
        entry=entry,
        sl=sl,
        tp=tp,
        score=0.0,
        reasons=[],
        mode=mode,
        bias=bias,
    )

    score, reasons = score_setup(ctx, idea)
    idea.score = score
    idea.reasons = reasons

    logger.info(
        "{} {} setup found — score {}/10, RR {}, bias {}",
        symbol.value, side.value, score, idea.risk_reward, bias.value,
    )
    return idea
