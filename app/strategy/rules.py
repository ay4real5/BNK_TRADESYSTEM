"""
Core strategy rules — Gold Sniper Pullback v1 (XAUUSD only).

Timeframes:
  - Bias determination:    1H  (EMA200)
  - Structure detection:   15m (swing HH/HL or LH/LL)
  - Entry trigger:         15m (pullback to VWAP or EMA20, continuation candle)

Rule pipeline (all must pass):
  1. Gold-only guard        — symbol == XAUUSD (when gold_only_mode=True)
  2. 1H bias                — price vs EMA200 → BULLISH or BEARISH
  3. 15m structure          — HH+HL (bullish) or LH+LL (bearish) must agree with bias
  4. ATR floor              — ATR(15m,14) > 0 (dead-market guard)
  5. Pullback anchor        — price at or below VWAP/EMA20 (buy) / at or above (sell)
  6. Continuation candle    — last candle body closes in trade direction
                              AND breaks prior candle's high (buy) or low (sell)
  7. RSI confirmation       — RSI recently crossed 50 in trade direction
  8. R:R gate               — reward/risk ≥ settings.min_rr_to_execute (default 1.5)
  9. Score gate             — handled by caller (min_score_to_execute = 8.0)
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
    add_vwap,
    compute_15m_structure,
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


def _is_continuation_candle(df: pd.DataFrame, side: Side) -> bool:
    """
    Sniper-grade entry confirmation:
      - Last candle body closes in the direction of the trade
        (close > open for BUY; close < open for SELL)
      - AND that close breaks the prior candle's extreme
        (close > prior_high for BUY; close < prior_low for SELL)

    Both conditions must hold — this filters out doji/indecision candles.
    """
    if len(df) < 2:
        return False
    last = df.iloc[-1]
    body_ok = (
        last["close"] > last["open"] if side == Side.BUY
        else last["close"] < last["open"]
    )
    breakout_ok = _candle_breakout(df, side)
    return body_ok and breakout_ok


def _is_pullback_to_anchor(
    price: float,
    vwap: float,
    ema20: float,
    side: Side,
    tolerance_pct: float = 0.003,
) -> bool:
    """
    Check if price has pulled back to VWAP or EMA20.

    BUY  → price at or below VWAP + tolerance  (came down to value area)
    SELL → price at or above VWAP - tolerance  (bounced up to value area)

    Falls back to EMA20 proximity if VWAP is 0.
    """
    # VWAP check (preferred anchor)
    if vwap > 0:
        if side == Side.BUY:
            # Price should be at or slightly below VWAP (value area buy)
            near_vwap = price <= vwap * (1 + tolerance_pct)
        else:
            near_vwap = price >= vwap * (1 - tolerance_pct)
        if near_vwap:
            return True
    # EMA20 fallback
    return _is_near_ema(price, ema20, tolerance_pct * 2)


def is_session_active(ts: datetime) -> tuple[bool, bool]:
    """Return (is_london, is_ny) based on UTC hour."""
    hour = ts.hour
    is_london = settings.london_open_utc <= hour < settings.london_close_utc
    is_ny = settings.ny_open_utc <= hour < settings.ny_close_utc
    return is_london, is_ny


def _session_label(is_london: bool, is_ny: bool) -> str:
    """Return a human-readable session tag for analytics."""
    if is_london and is_ny:
        return "overlap"
    if is_london:
        return "london"
    if is_ny:
        return "ny"
    return "unknown"


def evaluate_strategy(
    symbol: Symbol,
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    mode: Mode,
) -> TradeIdea | None:
    """
    Run the Gold Sniper Pullback v1 rule-set.

    Returns a TradeIdea if a clean pullback-continuation setup is found,
    otherwise None.
    """
    # ── Rule 1: Gold-only guard ───────────────────────────────────────────
    if settings.gold_only_mode and symbol != Symbol.XAUUSD:
        logger.debug("gold_only_mode: skipping {}", symbol.value)
        return None

    if len(candles_15m) < settings.ema_bias_period or len(candles_1h) < settings.ema_bias_period:
        logger.debug("{}: insufficient candles for strategy evaluation", symbol.value)
        return None

    df_1h  = candles_to_df(candles_1h)
    df_15m = candles_to_df(candles_15m)

    # Add technical indicators
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
    if settings.vwap_enabled:
        df_15m = add_vwap(df_15m)

    # ── Rule 2: 1H directional bias ───────────────────────────────────────
    bias = determine_bias(df_1h)
    if bias == Bias.NEUTRAL:
        return None

    side = Side.BUY if bias == Bias.BULLISH else Side.SELL

    # Current 15m values
    last = df_15m.iloc[-1]
    price  = float(last["close"])
    ema20  = float(last.get(f"ema_{settings.ema_fast_period}", 0) or 0)
    ema50  = float(last.get(f"ema_{settings.ema_slow_period}", 0) or 0)
    rsi_val = float(last.get(f"rsi_{settings.rsi_period}", 50) or 50)
    atr_val = float(last.get(f"atr_{settings.atr_period}", 0) or 0)
    vwap   = float(last.get("vwap", 0) or 0) if settings.vwap_enabled else 0.0
    rsi_col = f"rsi_{settings.rsi_period}"

    # ── Rule 3: 15m swing structure must agree with 1H bias ──────────────
    structure = compute_15m_structure(df_15m, settings.structure_lookback_candles)
    expected_structure = "bullish" if side == Side.BUY else "bearish"
    if structure != expected_structure:
        logger.debug(
            "{}: 15m structure {} disagrees with 1H bias {} — skip",
            symbol.value, structure, bias.value,
        )
        return None

    # ── Rule 4: ATR floor (dead-market guard) ────────────────────────────
    if atr_val == 0:
        return None

    # ── Rule 5: Pullback to VWAP or EMA20 ────────────────────────────────
    if not _is_pullback_to_anchor(price, vwap, ema20, side):
        logger.debug(
            "{}: no pullback to anchor (price={:.2f}, vwap={:.2f}, ema20={:.2f}) — skip",
            symbol.value, price, vwap, ema20,
        )
        return None

    # ── Rule 6: Continuation candle ──────────────────────────────────────
    if not _is_continuation_candle(df_15m, side):
        logger.debug("{}: no continuation candle — skip", symbol.value)
        return None

    # ── Rule 7: RSI cross confirmation ───────────────────────────────────
    if not _rsi_cross(df_15m, rsi_col, side):
        logger.debug("{}: RSI has not crossed 50 — skip", symbol.value)
        return None

    entry = price
    sl, tp = calc_sl_tp(side, entry, atr_val)

    # ── Rule 8: R:R hard gate ─────────────────────────────────────────────
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    rr = round(reward / risk, 2) if risk > 0 else 0.0
    if rr < settings.min_rr_to_execute:
        logger.debug("{}: R:R {:.2f} below minimum {:.2f} — skip", symbol.value, rr, settings.min_rr_to_execute)
        return None

    ts = df_15m.index[-1]
    ts_dt = ts if isinstance(ts, datetime) else ts.to_pydatetime()
    is_london, is_ny = is_session_active(ts_dt)

    volatility_regime = classify_volatility(df_15m)

    ctx = MarketContext(
        symbol=symbol,
        ts=ts_dt,
        bias=bias,
        price=price,
        spread=0.0,  # will be updated by caller
        atr_15m=atr_val,
        ema200_1h=float(df_1h[f"ema_{settings.ema_bias_period}"].iloc[-1] or 0),
        ema20_15m=ema20,
        ema50_15m=ema50,
        rsi_15m=rsi_val,
        vwap=vwap,
        structure_15m=structure,
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
        model_type=settings.gold_sniper_model_type,
        session_label=_session_label(is_london, is_ny),
    )

    score, reasons = score_setup(ctx, idea)
    idea.score = score
    idea.reasons = reasons

    logger.info(
        "{} {} setup found — score {}/10, RR {:.2f}, bias {}, session={}, model={}",
        symbol.value, side.value, score, rr, bias.value,
        idea.session_label, idea.model_type,
    )
    return idea
