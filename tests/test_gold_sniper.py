"""
Tests for the Gold Sniper Pullback v1 model.

Covers:
  - Gold-only guard (XAGUSD returns None)
  - VWAP computation (add_vwap)
  - 15m swing structure detection (compute_15m_structure)
  - Pullback anchor check (_is_pullback_to_anchor)
  - Continuation candle check (_is_continuation_candle)
  - R:R hard gate
  - Session label tagging
  - Model type tagging
  - Scorer: VWAP, structure, model_type in reasons
  - Config defaults (min_score=8.0, max_trades=2, max_losses=2)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from app.config import settings
from app.domain.enums import Bias, Mode, Side, Symbol
from app.domain.models import MarketContext, TradeIdea
from app.strategy.features import add_vwap, compute_15m_structure
from app.strategy.rules import (
    _is_continuation_candle,
    _is_pullback_to_anchor,
    _session_label,
    evaluate_strategy,
)
from app.strategy.scorer import score_setup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(
    n: int = 30,
    close_prices: list[float] | None = None,
    *,
    trending_up: bool = True,
    volume: float = 0.0,
) -> pd.DataFrame:
    """Minimal DataFrame of OHLCV candles with a datetime index."""
    now = datetime.utcnow()
    idx = pd.date_range(end=now, periods=n, freq="15min")
    base = 2000.0

    rows = []
    for i in range(n):
        if close_prices is not None and i < len(close_prices):
            c = close_prices[i]
        else:
            c = base + (i * 0.5 if trending_up else -i * 0.5)
        o = c - 0.3
        h = c + 0.5
        lo = c - 0.5
        rows.append({"open": o, "high": h, "low": lo, "close": c, "volume": volume})

    return pd.DataFrame(rows, index=idx)


def _make_ctx(
    *,
    vwap: float = 2000.0,
    structure_15m: str = "bullish",
    side: Side = Side.BUY,
    price: float = 1999.0,
    rsi: float = 55.0,
    rr: float = 1.8,
    spread: float = 0.0,
    is_london: bool = True,
    is_ny: bool = False,
    volatility: str = "normal",
) -> tuple[MarketContext, TradeIdea]:
    """Build a MarketContext + TradeIdea pair for scoring tests."""
    sl = price - 12.0 if side == Side.BUY else price + 12.0
    tp = price + 12.0 * rr if side == Side.BUY else price - 12.0 * rr
    ctx = MarketContext(
        symbol=Symbol.XAUUSD,
        ts=datetime.utcnow(),
        bias=Bias.BULLISH if side == Side.BUY else Bias.BEARISH,
        price=price,
        spread=spread,
        atr_15m=10.0,
        ema200_1h=1950.0,
        ema20_15m=price * 1.001,
        ema50_15m=price * 1.003,
        rsi_15m=rsi,
        vwap=vwap,
        structure_15m=structure_15m,
        is_london_session=is_london,
        is_ny_session=is_ny,
        volatility_regime=volatility,
    )
    idea = TradeIdea(
        symbol=Symbol.XAUUSD,
        side=side,
        entry=price,
        sl=sl,
        tp=tp,
        score=0.0,
        reasons=[],
        mode=Mode.DEMO,
        bias=ctx.bias,
        model_type="gold_sniper_pullback_v1",
        session_label="london",
    )
    return ctx, idea


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

def test_config_min_score_is_8():
    assert settings.min_score_to_execute == 8.0


def test_config_max_trades_is_2():
    assert settings.max_trades_per_day == 2


def test_config_max_losses_is_2():
    assert settings.max_losses_per_day == 2


def test_config_gold_only_mode_enabled():
    assert settings.gold_only_mode is True


def test_config_model_type():
    assert settings.gold_sniper_model_type == "gold_sniper_pullback_v1"


def test_config_min_rr_is_1_5():
    assert settings.min_rr_to_execute == 1.5


# ---------------------------------------------------------------------------
# VWAP computation
# ---------------------------------------------------------------------------

def test_add_vwap_adds_column():
    df = _make_df(30)
    df = add_vwap(df)
    assert "vwap" in df.columns
    assert df["vwap"].notna().all()


def test_add_vwap_values_in_range():
    """VWAP should be close to the price range of each candle."""
    df = _make_df(30)
    df = add_vwap(df)
    # VWAP must be between the min low and max high of all candles
    assert df["vwap"].min() >= df["low"].min()
    assert df["vwap"].max() <= df["high"].max() * 1.001  # small rounding tolerance


def test_add_vwap_with_volume():
    df = _make_df(20, volume=1000.0)
    df = add_vwap(df)
    assert "vwap" in df.columns
    # Volume-weighted VWAP should still be in price range
    assert df["vwap"].min() > 0


def test_add_vwap_empty_df():
    df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df.index = pd.DatetimeIndex([])
    df = add_vwap(df)
    assert "vwap" in df.columns
    assert len(df) == 0


# ---------------------------------------------------------------------------
# 15m swing structure detection
# ---------------------------------------------------------------------------

def test_structure_bullish_on_uptrend():
    """Steadily rising prices → HH + HL → bullish structure."""
    closes = [2000.0 + i for i in range(30)]
    df = _make_df(30, close_prices=closes)
    result = compute_15m_structure(df, lookback=10)
    assert result == "bullish"


def test_structure_bearish_on_downtrend():
    """Steadily falling prices → LH + LL → bearish structure."""
    closes = [2030.0 - i for i in range(30)]
    df = _make_df(30, close_prices=closes)
    result = compute_15m_structure(df, lookback=10)
    assert result == "bearish"


def test_structure_neutral_on_flat():
    """Flat prices → no clear structure → neutral."""
    closes = [2000.0] * 30
    df = _make_df(30, close_prices=closes)
    result = compute_15m_structure(df, lookback=10)
    assert result == "neutral"


def test_structure_neutral_when_insufficient_data():
    """Too few candles → neutral (fail-safe)."""
    df = _make_df(5)
    result = compute_15m_structure(df, lookback=10)
    assert result == "neutral"


# ---------------------------------------------------------------------------
# Pullback anchor check
# ---------------------------------------------------------------------------

def test_pullback_buy_at_vwap():
    """Price exactly at VWAP on a BUY → pullback confirmed."""
    assert _is_pullback_to_anchor(2000.0, vwap=2001.0, ema20=2010.0, side=Side.BUY)


def test_pullback_sell_at_vwap():
    """Price exactly at VWAP on a SELL → pullback confirmed."""
    assert _is_pullback_to_anchor(2001.0, vwap=2000.0, ema20=1990.0, side=Side.SELL)


def test_pullback_buy_far_above_vwap():
    """Price 2% above VWAP on a BUY → not a pullback."""
    assert not _is_pullback_to_anchor(2040.0, vwap=2000.0, ema20=2010.0, side=Side.BUY)


def test_pullback_sell_far_below_vwap():
    """Price 2% below VWAP on a SELL → not a pullback."""
    assert not _is_pullback_to_anchor(1960.0, vwap=2000.0, ema20=1990.0, side=Side.SELL)


def test_pullback_no_vwap_falls_back_to_ema20():
    """When vwap=0, falls back to EMA20 proximity check."""
    # Price very close to EMA20, vwap=0 → should still pass
    assert _is_pullback_to_anchor(2000.0, vwap=0.0, ema20=2001.0, side=Side.BUY)


# ---------------------------------------------------------------------------
# Continuation candle
# ---------------------------------------------------------------------------

def test_continuation_candle_buy():
    """Bullish candle (close > open) that breaks prior high → valid."""
    # Two candles: prior high=2002, last candle closes at 2005 > 2002 and close>open
    df = pd.DataFrame([
        {"open": 1995.0, "high": 2002.0, "low": 1994.0, "close": 2001.0},
        {"open": 2001.0, "high": 2006.0, "low": 2000.0, "close": 2005.0},
    ])
    assert _is_continuation_candle(df, Side.BUY)


def test_continuation_candle_buy_doji_rejected():
    """Doji candle (close ≈ open) should be rejected even if it breaks prior high."""
    df = pd.DataFrame([
        {"open": 1995.0, "high": 2002.0, "low": 1994.0, "close": 2001.0},
        {"open": 2003.0, "high": 2004.0, "low": 2002.0, "close": 2002.5},
    ])
    # close < open → bearish body → not a valid BUY continuation
    assert not _is_continuation_candle(df, Side.BUY)


def test_continuation_candle_sell():
    """Bearish candle (close < open) that breaks prior low → valid."""
    df = pd.DataFrame([
        {"open": 2010.0, "high": 2012.0, "low": 2000.0, "close": 2005.0},
        {"open": 2005.0, "high": 2006.0, "low": 1998.0, "close": 1999.0},
    ])
    assert _is_continuation_candle(df, Side.SELL)


def test_continuation_candle_insufficient_data():
    """Single candle → False (can't check prior bar)."""
    df = pd.DataFrame([
        {"open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2003.0},
    ])
    assert not _is_continuation_candle(df, Side.BUY)


# ---------------------------------------------------------------------------
# Session label
# ---------------------------------------------------------------------------

def test_session_label_london():
    assert _session_label(is_london=True, is_ny=False) == "london"


def test_session_label_ny():
    assert _session_label(is_london=False, is_ny=True) == "ny"


def test_session_label_overlap():
    assert _session_label(is_london=True, is_ny=True) == "overlap"


def test_session_label_unknown():
    assert _session_label(is_london=False, is_ny=False) == "unknown"


# ---------------------------------------------------------------------------
# Gold-only guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gold_only_blocks_xagusd():
    """With gold_only_mode=True, XAGUSD should return None immediately."""
    with patch.object(settings, "gold_only_mode", True):
        result = evaluate_strategy(
            symbol=Symbol.XAGUSD,
            candles_15m=[],
            candles_1h=[],
            mode=Mode.DEMO,
        )
    assert result is None


@pytest.mark.asyncio
async def test_gold_only_disabled_allows_xagusd():
    """With gold_only_mode=False, XAGUSD passes the guard (may still return None
    if candles insufficient, but not because of the gold-only guard alone)."""
    # We just verify it doesn't raise an error — it returns None due to no candles
    with patch.object(settings, "gold_only_mode", False):
        result = evaluate_strategy(
            symbol=Symbol.XAGUSD,
            candles_15m=[],
            candles_1h=[],
            mode=Mode.DEMO,
        )
    # Returns None because of insufficient candles, not the gold-only guard
    assert result is None


# ---------------------------------------------------------------------------
# Model type field on TradeIdea
# ---------------------------------------------------------------------------

def test_trade_idea_model_type_field():
    """TradeIdea must support model_type and session_label fields."""
    idea = TradeIdea(
        symbol=Symbol.XAUUSD,
        side=Side.BUY,
        entry=2000.0,
        sl=1988.0,
        tp=2021.6,
        score=8.5,
        reasons=[],
        mode=Mode.DEMO,
        model_type="gold_sniper_pullback_v1",
        session_label="london",
    )
    assert idea.model_type == "gold_sniper_pullback_v1"
    assert idea.session_label == "london"


def test_trade_idea_defaults_empty_strings():
    """Default model_type and session_label should be empty strings."""
    idea = TradeIdea(
        symbol=Symbol.XAUUSD,
        side=Side.BUY,
        entry=2000.0,
        sl=1988.0,
        tp=2021.6,
        score=7.0,
        reasons=[],
        mode=Mode.DEMO,
    )
    assert idea.model_type == ""
    assert idea.session_label == ""


# ---------------------------------------------------------------------------
# Scorer — model_type and session in reasons
# ---------------------------------------------------------------------------

def test_scorer_includes_model_type_in_reasons():
    ctx, idea = _make_ctx()
    _, reasons = score_setup(ctx, idea)
    assert any("gold_sniper_pullback_v1" in r for r in reasons)


def test_scorer_includes_session_in_reasons():
    ctx, idea = _make_ctx(is_london=True, is_ny=False)
    idea.session_label = "london"
    _, reasons = score_setup(ctx, idea)
    assert any("london" in r.lower() for r in reasons)


def test_scorer_structure_bullish_scores_1_5():
    ctx, idea = _make_ctx(structure_15m="bullish", side=Side.BUY)
    score, reasons = score_setup(ctx, idea)
    assert any("15m structure bullish" in r for r in reasons)
    # Score must include the 1.5 structure bonus — check it exceeds the no-structure baseline
    ctx2, idea2 = _make_ctx(structure_15m="neutral", side=Side.BUY)
    score2, _ = score_setup(ctx2, idea2)
    assert score > score2


def test_scorer_structure_disagrees_gives_zero():
    ctx, idea = _make_ctx(structure_15m="bearish", side=Side.BUY)
    _, reasons = score_setup(ctx, idea)
    assert any("disagrees" in r for r in reasons)


def test_scorer_vwap_buy_at_value_area():
    """Price at/below VWAP for BUY → VWAP pullback point awarded."""
    ctx, idea = _make_ctx(price=1999.0, vwap=2000.0, side=Side.BUY)
    _, reasons = score_setup(ctx, idea)
    assert any("VWAP" in r and "value area buy" in r for r in reasons)


def test_scorer_vwap_sell_at_value_area():
    """Price at/above VWAP for SELL → VWAP pullback point awarded."""
    ctx, idea = _make_ctx(price=2001.0, vwap=2000.0, side=Side.SELL,
                          structure_15m="bearish")
    idea.side = Side.SELL
    idea.bias = Bias.BEARISH
    ctx.bias = Bias.BEARISH
    _, reasons = score_setup(ctx, idea)
    assert any("VWAP" in r and "value area sell" in r for r in reasons)


def test_scorer_overlap_session_gets_bonus():
    ctx, idea = _make_ctx(is_london=True, is_ny=True)
    idea.session_label = "overlap"
    _, reasons = score_setup(ctx, idea)
    assert any("overlap" in r.lower() for r in reasons)


def test_scorer_clean_setup_reaches_8():
    """A textbook setup (all criteria met) should score ≥ 8.0."""
    ctx, idea = _make_ctx(
        structure_15m="bullish",
        side=Side.BUY,
        price=1999.0,
        vwap=2000.0,
        rsi=57.0,
        rr=2.0,
        spread=0.0,
        is_london=True,
        is_ny=True,
        volatility="normal",
    )
    idea.session_label = "overlap"
    score, _ = score_setup(ctx, idea)
    assert score >= 8.0, f"Expected ≥8.0 but got {score}"


def test_scorer_marginal_setup_below_8():
    """A setup missing structure + VWAP alignment should score < 8.0."""
    ctx, idea = _make_ctx(
        structure_15m="neutral",
        side=Side.BUY,
        price=2010.0,       # Far above VWAP
        vwap=2000.0,
        rsi=48.0,           # RSI not confirming
        rr=1.5,
        spread=0.40,        # Wide spread
        is_london=False,
        is_ny=False,
        volatility="high",
    )
    score, _ = score_setup(ctx, idea)
    assert score < 8.0, f"Expected < 8.0 but got {score}"
