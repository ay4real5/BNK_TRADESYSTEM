"""
Tests for the risk governor and locks.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest

from app.domain.enums import LockReason, Mode, Side, Symbol
from app.domain.errors import LockError, RiskViolation
from app.domain.models import RiskState, TradeIdea
from app.execution.safeguards import (
    check_min_rr,
    check_sl_tp_valid,
    check_spread,
    check_volatility,
    run_all_safeguards,
)


# ---------------------------------------------------------------------------
# RiskState model
# ---------------------------------------------------------------------------

def test_risk_state_not_locked_by_default():
    state = RiskState(date="2024-01-01")
    assert not state.is_locked


def test_risk_state_kill_switch():
    state = RiskState(date="2024-01-01", kill_switch=True)
    assert state.is_locked


def test_risk_state_paused():
    future = datetime.utcnow() + timedelta(minutes=30)
    state = RiskState(date="2024-01-01", paused_until_ts=future)
    assert state.is_locked


def test_risk_state_cooldown():
    future = datetime.utcnow() + timedelta(minutes=60)
    state = RiskState(date="2024-01-01", locked_until_ts=future, lock_reason=LockReason.COOLDOWN)
    assert state.is_locked


def test_risk_state_expired_cooldown():
    past = datetime.utcnow() - timedelta(minutes=1)
    state = RiskState(date="2024-01-01", locked_until_ts=past, lock_reason=LockReason.COOLDOWN)
    assert not state.is_locked


# ---------------------------------------------------------------------------
# Safeguards
# ---------------------------------------------------------------------------

def _make_idea(entry=2000.0, sl=1990.0, tp=2018.0, side=Side.BUY, score=7.0) -> TradeIdea:
    return TradeIdea(
        symbol=Symbol.XAUUSD,
        side=side,
        entry=entry,
        sl=sl,
        tp=tp,
        score=score,
        mode=Mode.PAPER,
    )


def test_check_spread_ok():
    check_spread(0.20, Symbol.XAUUSD)  # within default 0.50


def test_check_spread_too_wide():
    with pytest.raises(RiskViolation):
        check_spread(1.0, Symbol.XAUUSD)


def test_check_volatility_normal():
    check_volatility("normal")  # should not raise


def test_check_volatility_extreme():
    with pytest.raises(RiskViolation):
        check_volatility("extreme")


def test_check_sl_tp_valid_buy():
    idea = _make_idea(entry=2000, sl=1990, tp=2020, side=Side.BUY)
    check_sl_tp_valid(idea)  # no exception


def test_check_sl_tp_invalid_buy_sl_above_entry():
    idea = _make_idea(entry=2000, sl=2010, tp=2020, side=Side.BUY)
    with pytest.raises(RiskViolation):
        check_sl_tp_valid(idea)


def test_check_sl_tp_valid_sell():
    idea = _make_idea(entry=2000, sl=2010, tp=1980, side=Side.SELL)
    check_sl_tp_valid(idea)  # no exception


def test_check_sl_tp_invalid_sell_sl_below_entry():
    idea = _make_idea(entry=2000, sl=1990, tp=1980, side=Side.SELL)
    with pytest.raises(RiskViolation):
        check_sl_tp_valid(idea)


def test_check_min_rr_ok():
    idea = _make_idea(entry=2000, sl=1990, tp=2018)
    check_min_rr(idea, min_rr=1.5)  # RR = 1.8, should pass


def test_check_min_rr_too_low():
    idea = _make_idea(entry=2000, sl=1990, tp=2005)
    with pytest.raises(RiskViolation):
        check_min_rr(idea, min_rr=1.5)


def test_run_all_safeguards_ok():
    idea = _make_idea()
    run_all_safeguards(idea, spread=0.20, volatility_regime="normal")  # no exception


def test_run_all_safeguards_bad_spread():
    idea = _make_idea()
    with pytest.raises(RiskViolation):
        run_all_safeguards(idea, spread=5.0, volatility_regime="normal")
