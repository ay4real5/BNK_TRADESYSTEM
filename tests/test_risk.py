"""
Tests for the risk governor and locks.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.domain.enums import LockReason, Mode, Side, Symbol
from app.domain.errors import LockError, RiskViolation
from app.domain.models import AccountState, RiskState, TradeIdea
from app.execution.safeguards import (
    check_min_rr,
    check_sl_tp_valid,
    check_spread,
    check_volatility,
    run_all_safeguards,
)
from app.services import risk_manager


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


# ---------------------------------------------------------------------------
# RiskManager — compute_daily_loss_limit
# ---------------------------------------------------------------------------

def test_daily_loss_limit_pct_only():
    """2 % of $10 000 = -$200."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_daily_loss_pct=2.0,
        max_daily_loss_abs=None,
    ):
        assert risk_manager.compute_daily_loss_limit() == pytest.approx(-200.0)


def test_daily_loss_limit_abs_stricter():
    """Hard floor of -$150 is stricter than 2 % of $10 000 (-$200)."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_daily_loss_pct=2.0,
        max_daily_loss_abs=-150.0,
    ):
        assert risk_manager.compute_daily_loss_limit() == pytest.approx(-150.0)


def test_daily_loss_limit_pct_stricter():
    """Pct limit of -$200 is stricter than hard floor -$300, so pct wins."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_daily_loss_pct=2.0,
        max_daily_loss_abs=-300.0,
    ):
        assert risk_manager.compute_daily_loss_limit() == pytest.approx(-200.0)


def test_daily_loss_limit_explicit_balance():
    """Passing an explicit equity overrides the settings value."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_daily_loss_pct=2.0,
        max_daily_loss_abs=None,
    ):
        assert risk_manager.compute_daily_loss_limit(equity=5_000.0) == pytest.approx(-100.0)


# ---------------------------------------------------------------------------
# RiskManager — is_daily_loss_breached
# ---------------------------------------------------------------------------

def test_daily_loss_not_breached():
    """PnL of -$100 is within a -$200 limit → not breached."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_daily_loss_pct=2.0,
        max_daily_loss_abs=None,
    ):
        assert risk_manager.is_daily_loss_breached(-100.0) is False


def test_daily_loss_exactly_at_limit():
    """PnL exactly equal to the limit is considered breached (<=)."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_daily_loss_pct=2.0,
        max_daily_loss_abs=None,
    ):
        assert risk_manager.is_daily_loss_breached(-200.0) is True


def test_daily_loss_beyond_limit():
    """PnL worse than limit is breached."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_daily_loss_pct=2.0,
        max_daily_loss_abs=None,
    ):
        assert risk_manager.is_daily_loss_breached(-250.0) is True


def test_daily_loss_positive_pnl_not_breached():
    """Positive PnL is never a breach."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_daily_loss_pct=2.0,
        max_daily_loss_abs=None,
    ):
        assert risk_manager.is_daily_loss_breached(50.0) is False


# ---------------------------------------------------------------------------
# LockReason enum
# ---------------------------------------------------------------------------

def test_lock_reason_daily_loss_limit_exists():
    assert LockReason.DAILY_LOSS_LIMIT.value == "daily_loss_limit"


def test_lock_reason_total_drawdown_exists():
    assert LockReason.TOTAL_DRAWDOWN.value == "total_drawdown_exceeded"


# ---------------------------------------------------------------------------
# risk_manager — total drawdown
# ---------------------------------------------------------------------------

def test_total_drawdown_limit_calculation():
    """10% of $10 000 peak = -$1 000."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_total_drawdown_pct=10.0,
    ):
        assert risk_manager.compute_max_drawdown_limit(10_000.0) == pytest.approx(-1_000.0)


def test_total_drawdown_not_breached():
    """Equity $9 500 on peak $10 000 = 5% DD, limit 10% → not breached."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_total_drawdown_pct=10.0,
    ):
        assert risk_manager.is_total_drawdown_breached(9_500.0, 10_000.0) is False


def test_total_drawdown_breached():
    """Equity $8 900 on peak $10 000 = 11% DD, limit 10% → breached."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_total_drawdown_pct=10.0,
    ):
        assert risk_manager.is_total_drawdown_breached(8_900.0, 10_000.0) is True


def test_total_drawdown_exactly_at_limit():
    """Equity exactly at limit (-10%) → breached."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        max_total_drawdown_pct=10.0,
    ):
        assert risk_manager.is_total_drawdown_breached(9_000.0, 10_000.0) is True


# ---------------------------------------------------------------------------
# risk_manager — position sizing
# ---------------------------------------------------------------------------

def test_position_pnl_scaling():
    """0.5% risk on $10 000 equity = $50 risk; R:R 1.8 → $90 win."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        risk_per_trade_pct=0.5,
        tp_rr_ratio=1.8,
    ):
        sizing = risk_manager.compute_position_pnl(equity=10_000.0)
        assert sizing["win"] == pytest.approx(90.0)
        assert sizing["loss"] == pytest.approx(-50.0)


def test_position_pnl_grows_with_equity():
    """Sizing on $12 000 equity should be larger than on $10 000."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        risk_per_trade_pct=0.5,
        tp_rr_ratio=1.8,
    ):
        small = risk_manager.compute_position_pnl(equity=10_000.0)
        large = risk_manager.compute_position_pnl(equity=12_000.0)
        assert large["win"] > small["win"]
        assert large["loss"] < small["loss"]   # loss is negative; more negative = bigger


# ---------------------------------------------------------------------------
# AccountState model
# ---------------------------------------------------------------------------

def test_account_state_apply_pnl_win():
    """Winning trade increases balance, equity, and total PnL."""
    account = AccountState(
        starting_balance=10_000.0,
        balance=10_000.0,
        equity=10_000.0,
        peak_equity=10_000.0,
    )
    updated = account.apply_pnl(100.0)
    assert updated.balance == pytest.approx(10_100.0)
    assert updated.equity == pytest.approx(10_100.0)
    assert updated.peak_equity == pytest.approx(10_100.0)  # new high
    assert updated.total_pnl == pytest.approx(100.0)
    assert updated.drawdown_pct == pytest.approx(0.0)


def test_account_state_apply_pnl_loss():
    """Losing trade reduces balance/equity but preserves peak; drawdown > 0."""
    account = AccountState(
        starting_balance=10_000.0,
        balance=10_000.0,
        equity=10_000.0,
        peak_equity=10_000.0,
    )
    updated = account.apply_pnl(-200.0)
    assert updated.balance == pytest.approx(9_800.0)
    assert updated.equity == pytest.approx(9_800.0)
    assert updated.peak_equity == pytest.approx(10_000.0)  # peak unchanged
    assert updated.total_pnl == pytest.approx(-200.0)
    assert updated.drawdown_pct == pytest.approx(2.0)


def test_account_state_apply_pnl_new_peak():
    """Multiple wins push peak higher; DD returns to 0 on each new high."""
    account = AccountState(
        starting_balance=10_000.0,
        balance=10_200.0,
        equity=10_200.0,
        peak_equity=10_200.0,
    )
    updated = account.apply_pnl(300.0)
    assert updated.peak_equity == pytest.approx(10_500.0)
    assert updated.drawdown_pct == pytest.approx(0.0)


def test_account_state_immutable():
    """apply_pnl must return a new object, not mutate in place."""
    original = AccountState(balance=10_000.0, equity=10_000.0, peak_equity=10_000.0)
    updated = original.apply_pnl(-50.0)
    assert original.balance == pytest.approx(10_000.0)
    assert updated.balance == pytest.approx(9_950.0)


# ---------------------------------------------------------------------------
# AccountState — consecutive_losses tracking
# ---------------------------------------------------------------------------

def test_account_consecutive_losses_increments_on_loss():
    """consecutive_losses increments by 1 on each losing trade."""
    account = AccountState(
        balance=10_000.0, equity=10_000.0, peak_equity=10_000.0, consecutive_losses=0
    )
    a1 = account.apply_pnl(-50.0)
    assert a1.consecutive_losses == 1
    a2 = a1.apply_pnl(-50.0)
    assert a2.consecutive_losses == 2


def test_account_consecutive_losses_resets_on_win():
    """Any win resets the consecutive losses counter to 0."""
    account = AccountState(
        balance=10_000.0, equity=10_000.0, peak_equity=10_000.0, consecutive_losses=4
    )
    updated = account.apply_pnl(90.0)
    assert updated.consecutive_losses == 0


def test_account_consecutive_losses_unchanged_on_breakeven():
    """A zero-PnL trade (breakeven) resets the streak (pnl >= 0 branch)."""
    account = AccountState(
        balance=10_000.0, equity=10_000.0, peak_equity=10_000.0, consecutive_losses=2
    )
    updated = account.apply_pnl(0.0)
    assert updated.consecutive_losses == 0


# ---------------------------------------------------------------------------
# AccountState — equity_at_day_start preserved through apply_pnl
# ---------------------------------------------------------------------------

def test_account_equity_at_day_start_not_mutated_by_apply_pnl():
    """apply_pnl must NOT change equity_at_day_start (that's account_manager's job)."""
    account = AccountState(
        balance=10_000.0,
        equity=10_000.0,
        peak_equity=10_000.0,
        equity_at_day_start=10_000.0,
    )
    updated = account.apply_pnl(-300.0)
    assert updated.equity_at_day_start == pytest.approx(10_000.0)  # unchanged


# ---------------------------------------------------------------------------
# risk_manager — intraday drawdown stop
# ---------------------------------------------------------------------------

def test_intraday_dd_limit_calculation():
    """5% of $10 000 day-open = -$500."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        intraday_dd_stop_pct=5.0,
    ):
        assert risk_manager.compute_intraday_dd_limit(10_000.0) == pytest.approx(-500.0)


def test_intraday_dd_not_breached():
    """Equity at $9 600 from $10 000 open = 4% intraday drop, limit 5% → not breached."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        intraday_dd_stop_pct=5.0,
    ):
        assert risk_manager.is_intraday_dd_breached(9_600.0, 10_000.0) is False


def test_intraday_dd_breached():
    """Equity at $9_400 from $10 000 open = 6% intraday drop, limit 5% → breached."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        intraday_dd_stop_pct=5.0,
    ):
        assert risk_manager.is_intraday_dd_breached(9_400.0, 10_000.0) is True


def test_intraday_dd_exactly_at_limit():
    """Equity at exactly -5% from open → breached."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        intraday_dd_stop_pct=5.0,
    ):
        assert risk_manager.is_intraday_dd_breached(9_500.0, 10_000.0) is True


def test_intraday_dd_above_open_not_breached():
    """If today's equity is above day-open (profitable session), never breached."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        intraday_dd_stop_pct=5.0,
    ):
        assert risk_manager.is_intraday_dd_breached(10_500.0, 10_000.0) is False


# ---------------------------------------------------------------------------
# risk_manager — consecutive-loss position scaling
# ---------------------------------------------------------------------------

def test_position_pnl_no_scaling_below_threshold():
    """Below 3 consecutive losses, full 0.5% risk applies."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        risk_per_trade_pct=0.5,
        tp_rr_ratio=1.8,
        consecutive_loss_threshold=3,
        consecutive_loss_scale_factor=0.5,
    ):
        sizing = risk_manager.compute_position_pnl(equity=10_000.0, consecutive_losses=2)
        assert sizing["risk_pct"] == pytest.approx(0.5)
        assert sizing["loss"] == pytest.approx(-50.0)


def test_position_pnl_scaled_at_threshold():
    """At exactly 3 consecutive losses, risk is halved (0.5% → 0.25%)."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        risk_per_trade_pct=0.5,
        tp_rr_ratio=1.8,
        consecutive_loss_threshold=3,
        consecutive_loss_scale_factor=0.5,
    ):
        sizing = risk_manager.compute_position_pnl(equity=10_000.0, consecutive_losses=3)
        assert sizing["risk_pct"] == pytest.approx(0.25)
        assert sizing["loss"] == pytest.approx(-25.0)
        assert sizing["win"] == pytest.approx(45.0)   # 0.25% * 1.8 * $10 000 = $45


def test_position_pnl_scaled_beyond_threshold():
    """5 consecutive losses still uses the halved risk rate (not further reduced)."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        risk_per_trade_pct=0.5,
        tp_rr_ratio=1.8,
        consecutive_loss_threshold=3,
        consecutive_loss_scale_factor=0.5,
    ):
        sizing = risk_manager.compute_position_pnl(equity=10_000.0, consecutive_losses=5)
        assert sizing["risk_pct"] == pytest.approx(0.25)


def test_position_pnl_zero_consecutive_losses_full_risk():
    """Fresh streak (0 losses) always uses full risk %."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        risk_per_trade_pct=0.5,
        tp_rr_ratio=1.8,
        consecutive_loss_threshold=3,
        consecutive_loss_scale_factor=0.5,
    ):
        sizing = risk_manager.compute_position_pnl(equity=10_000.0, consecutive_losses=0)
        assert sizing["risk_pct"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# LockReason enum additions
# ---------------------------------------------------------------------------

def test_lock_reason_intraday_dd_stop_exists():
    assert LockReason.INTRADAY_DD_STOP.value == "intraday_drawdown_stop"


# ---------------------------------------------------------------------------
# settings.effective_risk_pct helper
# ---------------------------------------------------------------------------

def test_settings_effective_risk_pct_normal():
    """Below threshold → full base risk."""
    from app.config import Settings
    s = Settings(risk_per_trade_pct=0.5, consecutive_loss_threshold=3, consecutive_loss_scale_factor=0.5)
    assert s.effective_risk_pct(0) == pytest.approx(0.5)
    assert s.effective_risk_pct(2) == pytest.approx(0.5)


def test_settings_effective_risk_pct_scaled():
    """At/above threshold → halved risk."""
    from app.config import Settings
    s = Settings(risk_per_trade_pct=0.5, consecutive_loss_threshold=3, consecutive_loss_scale_factor=0.5)
    assert s.effective_risk_pct(3) == pytest.approx(0.25)
    assert s.effective_risk_pct(10) == pytest.approx(0.25)
