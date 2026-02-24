"""
Tests for the strategy module.

Uses SyntheticDataProvider candles as input — no real market data needed.
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from app.data.market_data import candles_to_df
from app.data.providers.ohlc_csv import SyntheticDataProvider
from app.domain.enums import Bias, Side, Symbol
from app.strategy.features import add_all_features, add_ema, add_rsi, add_atr
from app.strategy.regime import classify_volatility
from app.strategy.rules import determine_bias
from app.strategy.risk import calc_sl_tp, calc_position_size


# ---------------------------------------------------------------------------
# Feature tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ema_added():
    provider = SyntheticDataProvider(seed=1)
    candles = await provider.fetch_candles(Symbol.XAUUSD, "15m", count=250)
    df = candles_to_df(candles)
    df = add_ema(df, 20)
    assert "ema_20" in df.columns
    # Last value should be a float, not NaN
    assert pd.notna(df["ema_20"].iloc[-1])


@pytest.mark.asyncio
async def test_rsi_range():
    provider = SyntheticDataProvider(seed=1)
    candles = await provider.fetch_candles(Symbol.XAUUSD, "15m", count=250)
    df = candles_to_df(candles)
    df = add_rsi(df, 14)
    rsi_vals = df["rsi_14"].dropna()
    assert (rsi_vals >= 0).all()
    assert (rsi_vals <= 100).all()


@pytest.mark.asyncio
async def test_atr_positive():
    provider = SyntheticDataProvider(seed=1)
    candles = await provider.fetch_candles(Symbol.XAUUSD, "15m", count=250)
    df = candles_to_df(candles)
    df = add_atr(df, 14)
    atr_vals = df["atr_14"].dropna()
    assert (atr_vals > 0).all()


@pytest.mark.asyncio
async def test_add_all_features_columns_present():
    provider = SyntheticDataProvider(seed=2)
    candles = await provider.fetch_candles(Symbol.XAGUSD, "15m", count=300)
    df = candles_to_df(candles)
    df = add_all_features(df)
    for col in ["ema_200", "ema_20", "ema_50", "rsi_14", "atr_14"]:
        assert col in df.columns, f"Missing column: {col}"


# ---------------------------------------------------------------------------
# Bias determination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_determine_bias_returns_valid():
    provider = SyntheticDataProvider(seed=3)
    candles = await provider.fetch_candles(Symbol.XAUUSD, "1h", count=300)
    df = candles_to_df(candles)
    df = add_all_features(df)
    bias = determine_bias(df)
    assert bias in (Bias.BULLISH, Bias.BEARISH, Bias.NEUTRAL)


# ---------------------------------------------------------------------------
# Regime
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_classify_volatility_normal():
    provider = SyntheticDataProvider(seed=4)
    candles = await provider.fetch_candles(Symbol.XAUUSD, "15m", count=300)
    df = candles_to_df(candles)
    df = add_atr(df, 14)
    regime = classify_volatility(df)
    assert regime in ("low", "normal", "high", "extreme")


# ---------------------------------------------------------------------------
# Risk / position sizing
# ---------------------------------------------------------------------------

def test_calc_sl_tp_buy():
    sl, tp = calc_sl_tp(Side.BUY, entry=2000.0, atr=10.0, sl_multiplier=1.2, rr_ratio=1.8)
    assert sl < 2000.0
    assert tp > 2000.0
    risk = 2000.0 - sl
    reward = tp - 2000.0
    assert abs(reward / risk - 1.8) < 0.01


def test_calc_sl_tp_sell():
    sl, tp = calc_sl_tp(Side.SELL, entry=2000.0, atr=10.0, sl_multiplier=1.2, rr_ratio=1.8)
    assert sl > 2000.0
    assert tp < 2000.0


def test_position_size_xauusd():
    size = calc_position_size(
        account_balance=10000,
        entry=2000.0,
        sl=1988.0,  # 12 point stop
        symbol=Symbol.XAUUSD,
        risk_pct=0.5,
    )
    assert size >= 0.0
    assert size < 10.0  # should be a fraction of a lot for small account


def test_position_size_zero_sl():
    """If entry == SL, size must be 0 (avoid division by zero)."""
    size = calc_position_size(
        account_balance=10000,
        entry=2000.0,
        sl=2000.0,
        symbol=Symbol.XAUUSD,
        risk_pct=0.5,
    )
    assert size == 0.0


def test_rr_calculation():
    from app.domain.enums import Mode
    from app.domain.models import TradeIdea
    idea = TradeIdea(
        symbol=Symbol.XAUUSD,
        side=Side.BUY,
        entry=2000.0,
        sl=1990.0,
        tp=2018.0,
        score=7.0,
        mode=Mode.PAPER,
    )
    assert idea.risk_reward == pytest.approx(1.8, abs=0.01)
