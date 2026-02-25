"""
Tests for Mode C — Defensive Core + Statistical Expansion Layer.

Coverage:
- Expansion activation gate conditions
- Expansion exit conditions
- Defensive fallback behaviour
- risk_manager expansion-aware sizing
- ExpansionState model
- rolling stats helper
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.models import AccountState, ExpansionState
from app.services import expansion_manager, risk_manager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _account(equity: float = 10_000.0, peak: float = 10_000.0, consec: int = 0) -> AccountState:
    return AccountState(
        starting_balance=10_000.0,
        balance=equity,
        equity=equity,
        peak_equity=peak,
        equity_at_day_start=10_000.0,
        total_pnl=equity - 10_000.0,
        consecutive_losses=consec,
    )


def _stats(
    total: int = 30,
    wins: int = 20,
    win_rate: float = 0.67,
    max_dd_pct: float = 1.5,
) -> dict:
    losses = total - wins
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "max_dd_pct": max_dd_pct,
        "pnls": [50.0] * wins + [-30.0] * losses,
    }


# ---------------------------------------------------------------------------
# ExpansionState model
# ---------------------------------------------------------------------------

def test_expansion_state_defaults():
    state = ExpansionState()
    assert state.active is False
    assert state.trades_in_window == 0
    assert state.consecutive_losses == 0
    assert state.exit_reason is None
    assert state.atr_spike_active is False


def test_expansion_state_model_copy():
    state = ExpansionState()
    updated = state.model_copy(update={"active": True, "trades_in_window": 5})
    assert updated.active is True
    assert updated.trades_in_window == 5
    assert state.active is False   # original unchanged (immutable copy)


# ---------------------------------------------------------------------------
# Activation gate: all conditions must pass
# ---------------------------------------------------------------------------

def test_activation_passes_all_gates():
    account = _account(equity=10_000.0, peak=10_000.0)  # at peak
    stats = _stats(total=30, win_rate=0.67, max_dd_pct=1.5)
    state = ExpansionState(atr_spike_active=False)
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_min_win_rate=0.60,
        expansion_max_dd_pct=3.0,
        expansion_min_trades=30,
    ):
        assert expansion_manager._check_activation_conditions(stats, account, state) is True


def test_activation_fails_insufficient_trades():
    account = _account()
    stats = _stats(total=15)  # below min_trades=30
    state = ExpansionState()
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_min_trades=30,
    ):
        assert expansion_manager._check_activation_conditions(stats, account, state) is False


def test_activation_fails_low_win_rate():
    account = _account(equity=10_000.0, peak=10_000.0)
    stats = _stats(total=30, win_rate=0.55)  # below 0.60 threshold
    state = ExpansionState()
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_min_win_rate=0.60,
        expansion_max_dd_pct=3.0,
        expansion_min_trades=30,
    ):
        assert expansion_manager._check_activation_conditions(stats, account, state) is False


def test_activation_fails_high_drawdown():
    account = _account(equity=10_000.0, peak=10_000.0)
    stats = _stats(total=30, win_rate=0.67, max_dd_pct=4.0)  # above 3.0% threshold
    state = ExpansionState()
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_min_win_rate=0.60,
        expansion_max_dd_pct=3.0,
        expansion_min_trades=30,
    ):
        assert expansion_manager._check_activation_conditions(stats, account, state) is False


def test_activation_fails_equity_below_peak():
    account = _account(equity=9_800.0, peak=10_000.0)  # not at high
    stats = _stats(total=30, win_rate=0.67, max_dd_pct=1.5)
    state = ExpansionState()
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_min_win_rate=0.60,
        expansion_max_dd_pct=3.0,
        expansion_min_trades=30,
    ):
        assert expansion_manager._check_activation_conditions(stats, account, state) is False


def test_activation_fails_atr_spike():
    account = _account(equity=10_000.0, peak=10_000.0)
    stats = _stats(total=30, win_rate=0.67, max_dd_pct=1.5)
    state = ExpansionState(atr_spike_active=True)  # spike active
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_min_win_rate=0.60,
        expansion_max_dd_pct=3.0,
        expansion_min_trades=30,
    ):
        assert expansion_manager._check_activation_conditions(stats, account, state) is False


# ---------------------------------------------------------------------------
# Expansion internal helpers: _activate / _deactivate / _update_window
# ---------------------------------------------------------------------------

def test_activate_sets_fields():
    state = ExpansionState()
    account = _account(equity=10_500.0)
    activated = expansion_manager._activate(state, account)
    assert activated.active is True
    assert activated.start_equity == 10_500.0
    assert activated.trades_in_window == 0
    assert activated.consecutive_losses == 0
    assert activated.activated_at is not None


def test_deactivate_clears_active():
    state = ExpansionState(active=True, trades_in_window=7, consecutive_losses=1)
    deactivated = expansion_manager._deactivate(state, "consecutive_losses (2)")
    assert deactivated.active is False
    assert deactivated.exit_reason == "consecutive_losses (2)"
    assert deactivated.trades_in_window == 7  # preserved for audit


def test_update_window_win_resets_streak():
    state = ExpansionState(active=True, trades_in_window=3, consecutive_losses=2)
    account = _account()
    updated = expansion_manager._update_window(state, account, is_win=True)
    assert updated.trades_in_window == 4
    assert updated.consecutive_losses == 0


def test_update_window_loss_increments_streak():
    state = ExpansionState(active=True, trades_in_window=3, consecutive_losses=1)
    account = _account()
    updated = expansion_manager._update_window(state, account, is_win=False)
    assert updated.trades_in_window == 4
    assert updated.consecutive_losses == 2


# ---------------------------------------------------------------------------
# Exit conditions
# ---------------------------------------------------------------------------

def test_exit_window_exhausted():
    state = ExpansionState(active=True, trades_in_window=20, start_equity=10_000.0)
    account = _account()
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_max_trades=20,
        expansion_exit_consec_losses=3,
        expansion_exit_dd_pct=3.0,
    ):
        should_exit, reason = expansion_manager._check_exit_conditions(state, account)
    assert should_exit is True
    assert "window_exhausted" in reason


def test_exit_consecutive_losses():
    state = ExpansionState(active=True, trades_in_window=5, consecutive_losses=3,
                           start_equity=10_000.0)
    account = _account()
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_max_trades=20,
        expansion_exit_consec_losses=3,
        expansion_exit_dd_pct=3.0,
    ):
        should_exit, reason = expansion_manager._check_exit_conditions(state, account)
    assert should_exit is True
    assert "consecutive_losses" in reason


def test_exit_drawdown_from_start():
    # Started expansion at $10 000, now at $9 650 → 3.5% drawdown > 3.0% limit
    state = ExpansionState(active=True, trades_in_window=5, consecutive_losses=0,
                           start_equity=10_000.0)
    account = _account(equity=9_650.0)
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_max_trades=20,
        expansion_exit_consec_losses=3,
        expansion_exit_dd_pct=3.0,
    ):
        should_exit, reason = expansion_manager._check_exit_conditions(state, account)
    assert should_exit is True
    assert "drawdown_from_start" in reason


def test_no_exit_within_bounds():
    state = ExpansionState(active=True, trades_in_window=5, consecutive_losses=2,
                           start_equity=10_000.0)
    account = _account(equity=9_800.0)  # only 2% drawdown from start
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_max_trades=20,
        expansion_exit_consec_losses=3,
        expansion_exit_dd_pct=3.0,
    ):
        should_exit, _ = expansion_manager._check_exit_conditions(state, account)
    assert should_exit is False


def test_exit_atr_spike():
    state = ExpansionState(active=True, trades_in_window=3, consecutive_losses=0,
                           start_equity=10_000.0, atr_spike_active=True)
    account = _account()
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_max_trades=20,
        expansion_exit_consec_losses=3,
        expansion_exit_dd_pct=3.0,
    ):
        should_exit, reason = expansion_manager._check_exit_conditions(state, account)
    assert should_exit is True
    assert reason == "atr_spike"


# ---------------------------------------------------------------------------
# effective_risk_pct (expansion_manager helper)
# ---------------------------------------------------------------------------

def test_effective_risk_expansion_always_full():
    """In expansion mode, consecutive losses do NOT reduce risk."""
    state = ExpansionState(active=True)
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_risk_pct=0.9,
        defensive_risk_pct=0.5,
        consecutive_loss_threshold=3,
        consecutive_loss_scale_factor=0.5,
    ):
        assert expansion_manager.effective_risk_pct(state, consecutive_losses=5) == pytest.approx(0.9)


def test_effective_risk_defensive_normal():
    state = ExpansionState(active=False)
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_risk_pct=0.9,
        defensive_risk_pct=0.5,
        consecutive_loss_threshold=3,
        consecutive_loss_scale_factor=0.5,
    ):
        assert expansion_manager.effective_risk_pct(state, consecutive_losses=0) == pytest.approx(0.5)


def test_effective_risk_defensive_scaled_after_streak():
    state = ExpansionState(active=False)
    with patch.multiple(
        "app.services.expansion_manager.settings",
        expansion_risk_pct=0.9,
        defensive_risk_pct=0.5,
        consecutive_loss_threshold=3,
        consecutive_loss_scale_factor=0.5,
    ):
        assert expansion_manager.effective_risk_pct(state, consecutive_losses=3) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# risk_manager — expansion-aware compute_position_pnl
# ---------------------------------------------------------------------------

def test_position_pnl_expansion_mode():
    """Expansion mode uses 0.9% — higher risk, higher potential."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        expansion_risk_pct=0.9,
        defensive_risk_pct=0.5,
        tp_rr_ratio=1.8,
        consecutive_loss_threshold=3,
        consecutive_loss_scale_factor=0.5,
    ):
        sizing = risk_manager.compute_position_pnl(equity=10_000.0,
                                                    consecutive_losses=0,
                                                    expansion_active=True)
    assert sizing["mode"] == "expansion"
    assert sizing["risk_pct"] == pytest.approx(0.9)
    assert sizing["loss"] == pytest.approx(-90.0)
    assert sizing["win"] == pytest.approx(162.0)   # 90 * 1.8


def test_position_pnl_defensive_mode():
    """Defensive mode uses 0.5% base risk."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        expansion_risk_pct=0.9,
        defensive_risk_pct=0.5,
        tp_rr_ratio=1.8,
        consecutive_loss_threshold=3,
        consecutive_loss_scale_factor=0.5,
    ):
        sizing = risk_manager.compute_position_pnl(equity=10_000.0,
                                                    consecutive_losses=0,
                                                    expansion_active=False)
    assert sizing["mode"] == "defensive"
    assert sizing["risk_pct"] == pytest.approx(0.5)
    assert sizing["loss"] == pytest.approx(-50.0)
    assert sizing["win"] == pytest.approx(90.0)


def test_position_pnl_expansion_beats_defensive():
    """Expansion sizing is strictly larger than defensive on same equity."""
    with patch.multiple(
        "app.services.risk_manager.settings",
        account_balance=10_000.0,
        expansion_risk_pct=0.9,
        defensive_risk_pct=0.5,
        tp_rr_ratio=1.8,
        consecutive_loss_threshold=3,
        consecutive_loss_scale_factor=0.5,
    ):
        exp = risk_manager.compute_position_pnl(equity=10_000.0, expansion_active=True)
        dfn = risk_manager.compute_position_pnl(equity=10_000.0, expansion_active=False)
    assert exp["win"] > dfn["win"]
    assert exp["loss"] < dfn["loss"]   # more negative = larger loss risk


# ---------------------------------------------------------------------------
# after_trade integration (with mocked storage)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_after_trade_stays_defensive_below_threshold():
    """With < 30 trades in window, stays in defensive mode."""
    account = _account(equity=10_000.0, peak=10_000.0)
    sparse_stats = {"total": 10, "wins": 8, "losses": 2, "win_rate": 0.8,
                    "max_dd_pct": 0.5, "pnls": []}
    initial_state = ExpansionState(active=False)

    with (
        patch("app.services.expansion_manager.load_expansion_state",
              new=AsyncMock(return_value=initial_state)),
        patch("app.services.expansion_manager.save_expansion_state",
              new=AsyncMock()),
        patch("app.services.expansion_manager.get_rolling_trade_stats",
              new=AsyncMock(return_value=sparse_stats)),
        patch.multiple(
            "app.services.expansion_manager.settings",
            expansion_min_trades=30,
            expansion_min_win_rate=0.60,
            expansion_max_dd_pct=3.0,
            expansion_max_trades=20,
            expansion_exit_consec_losses=3,
            expansion_exit_dd_pct=3.0,
            expansion_risk_pct=0.9,
            defensive_risk_pct=0.5,
        ),
    ):
        result = await expansion_manager.after_trade(account, is_win=True)
    assert result.active is False


@pytest.mark.asyncio
async def test_after_trade_activates_when_conditions_met():
    """All four activation gates pass → expansion activates."""
    account = _account(equity=10_000.0, peak=10_000.0)  # at peak
    good_stats = {"total": 35, "wins": 23, "losses": 12, "win_rate": 0.66,
                  "max_dd_pct": 1.2, "pnls": []}
    initial_state = ExpansionState(active=False, atr_spike_active=False)

    with (
        patch("app.services.expansion_manager.load_expansion_state",
              new=AsyncMock(return_value=initial_state)),
        patch("app.services.expansion_manager.save_expansion_state",
              new=AsyncMock()),
        patch("app.services.expansion_manager.get_rolling_trade_stats",
              new=AsyncMock(return_value=good_stats)),
        patch.multiple(
            "app.services.expansion_manager.settings",
            expansion_min_trades=30,
            expansion_min_win_rate=0.60,
            expansion_max_dd_pct=3.0,
            expansion_max_trades=20,
            expansion_exit_consec_losses=3,
            expansion_exit_dd_pct=3.0,
            expansion_risk_pct=0.9,
            defensive_risk_pct=0.5,
        ),
    ):
        result = await expansion_manager.after_trade(account, is_win=True)
    assert result.active is True
    assert result.start_equity == pytest.approx(10_000.0)


@pytest.mark.asyncio
async def test_after_trade_exits_expansion_on_three_losses():
    """Three consecutive losses in expansion → auto-exit."""
    account = _account(equity=9_970.0)  # minor loss, within dd limit
    # Already in expansion with 2 consecutive losses; this trade is a loss → 3rd
    initial_state = ExpansionState(
        active=True, trades_in_window=5, consecutive_losses=2,
        start_equity=10_000.0,
    )

    with (
        patch("app.services.expansion_manager.load_expansion_state",
              new=AsyncMock(return_value=initial_state)),
        patch("app.services.expansion_manager.save_expansion_state",
              new=AsyncMock()),
        patch.multiple(
            "app.services.expansion_manager.settings",
            expansion_max_trades=20,
            expansion_exit_consec_losses=3,
            expansion_exit_dd_pct=3.0,
            expansion_risk_pct=0.9,
            defensive_risk_pct=0.5,
        ),
    ):
        result = await expansion_manager.after_trade(account, is_win=False)
    assert result.active is False
    assert "consecutive_losses" in (result.exit_reason or "")


@pytest.mark.asyncio
async def test_after_trade_exits_expansion_on_drawdown():
    """Equity drops 4% from expansion start → exit."""
    account = _account(equity=9_580.0)   # 4.2% below start of $10 000
    initial_state = ExpansionState(
        active=True, trades_in_window=8, consecutive_losses=0,
        start_equity=10_000.0,
    )

    with (
        patch("app.services.expansion_manager.load_expansion_state",
              new=AsyncMock(return_value=initial_state)),
        patch("app.services.expansion_manager.save_expansion_state",
              new=AsyncMock()),
        patch.multiple(
            "app.services.expansion_manager.settings",
            expansion_max_trades=20,
            expansion_exit_consec_losses=3,
            expansion_exit_dd_pct=3.0,
            expansion_risk_pct=0.9,
            defensive_risk_pct=0.5,
        ),
    ):
        result = await expansion_manager.after_trade(account, is_win=False)
    assert result.active is False
    assert "drawdown_from_start" in (result.exit_reason or "")
