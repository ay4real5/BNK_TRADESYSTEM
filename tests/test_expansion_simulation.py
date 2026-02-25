"""
Mode C — End-to-End Lifecycle Simulation.

This test drives the expansion state machine through a complete realistic
scenario using stateful mocks that preserve state between calls (just as
the real SQLite layer would).

Simulation sequence
-------------------
Phase 1 — Warm-up (30 trades, 22W / 8L ≈ 73% win rate, low drawdown)
  • Each call to after_trade sees real rolling stats built from the window
  • At trade 30 the activation gates open → Expansion Mode activates
  • Verified: risk_mode = "expansion", risk_pct = 0.9%

Phase 2 — Inside expansion (simulate 2 consecutive losses)
  • Trade 31: LOSS  → consecutive_losses = 1, still in expansion
  • Trade 32: LOSS  → consecutive_losses = 2, threshold hit → EXIT
  • Verified: risk_mode back to "defensive", risk_pct = 0.5%

Phase 3 — Defensive fallback confirmation
  • Explicit assertion that risk_manager.compute_position_pnl uses
    defensive sizing immediately after deactivation
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.domain.models import AccountState, ExpansionState
from app.services import expansion_manager, risk_manager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _account(equity: float = 10_500.0, peak: float = 10_500.0) -> AccountState:
    """Return an account that is at its equity peak (required for activation)."""
    return AccountState(
        starting_balance=10_000.0,
        balance=equity,
        equity=equity,
        peak_equity=peak,          # equity == peak → new high gate passes
        equity_at_day_start=10_000.0,
        total_pnl=equity - 10_000.0,
        consecutive_losses=0,
    )


def _good_stats(total: int = 30) -> dict:
    """30 closed trades: 22 wins, 8 losses → 73.3% win rate, 1.2% max DD."""
    wins = 22
    losses = total - wins
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total, 4),
        "max_dd_pct": 1.2,
        "pnls": [90.0] * wins + [-50.0] * losses,
    }


_EXPANSION_SETTINGS = dict(
    expansion_min_trades=30,
    expansion_min_win_rate=0.60,
    expansion_max_dd_pct=3.0,
    expansion_max_trades=20,
    expansion_exit_consec_losses=3,
    expansion_exit_dd_pct=3.0,
    expansion_risk_pct=0.9,
    defensive_risk_pct=0.5,
    consecutive_loss_threshold=3,
    consecutive_loss_scale_factor=0.5,
    tp_rr_ratio=1.8,
    account_balance=10_000.0,
)


# ---------------------------------------------------------------------------
# Core simulation: stateful mock that persists ExpansionState between calls
# ---------------------------------------------------------------------------

class _StatefulExpansion:
    """
    Simulates the DB-backed expansion storage with in-memory state.
    Pass instances of load/save as AsyncMock side_effects.
    """

    def __init__(self) -> None:
        self._state = ExpansionState()

    async def load(self, **_kw) -> ExpansionState:
        return self._state

    async def save(self, state: ExpansionState, **_kw) -> None:
        self._state = state

    @property
    def current(self) -> ExpansionState:
        return self._state


# ---------------------------------------------------------------------------
# Phase 1 — Expansion activates after 30 qualifying trades
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase1_expansion_activates_after_qualifying_window():
    """
    After 30 trades with 73% win rate and low drawdown, expansion mode
    must activate on the next after_trade call.
    """
    db = _StatefulExpansion()
    account = _account(equity=10_500.0, peak=10_500.0)
    stats = _good_stats(total=30)

    with (
        patch("app.services.expansion_manager.load_expansion_state",
              new=AsyncMock(side_effect=db.load)),
        patch("app.services.expansion_manager.save_expansion_state",
              new=AsyncMock(side_effect=db.save)),
        patch("app.services.expansion_manager.get_rolling_trade_stats",
              new=AsyncMock(return_value=stats)),
        patch.multiple("app.services.expansion_manager.settings", **_EXPANSION_SETTINGS),
    ):
        result = await expansion_manager.after_trade(account, is_win=True)

    assert result.active is True, "Expansion should have activated"
    assert result.start_equity == pytest.approx(10_500.0)
    assert result.trades_in_window == 0      # counter resets at activation
    assert result.consecutive_losses == 0


@pytest.mark.asyncio
async def test_phase1_risk_increases_to_expansion_rate():
    """Once active, compute_position_pnl must return 0.9% risk (not 0.5%)."""
    state = ExpansionState(active=True, start_equity=10_500.0)

    with patch.multiple("app.services.risk_manager.settings", **_EXPANSION_SETTINGS):
        sizing = risk_manager.compute_position_pnl(
            equity=10_500.0,
            consecutive_losses=0,
            expansion_active=state.active,
        )

    assert sizing["mode"] == "expansion"
    assert sizing["risk_pct"] == pytest.approx(0.9)
    # 10_500 * 0.9% = 94.50 risk; win = 94.50 * 1.8 = 170.10
    assert sizing["loss"] == pytest.approx(-94.5)
    assert sizing["win"] == pytest.approx(170.1)


@pytest.mark.asyncio
async def test_phase1_does_not_activate_with_sparse_history():
    """
    With only 15 trades in history (below the 30-trade minimum), expansion
    must NOT activate regardless of win rate.
    """
    db = _StatefulExpansion()
    account = _account()
    sparse_stats = _good_stats(total=15)  # only 15 trades — below threshold

    with (
        patch("app.services.expansion_manager.load_expansion_state",
              new=AsyncMock(side_effect=db.load)),
        patch("app.services.expansion_manager.save_expansion_state",
              new=AsyncMock(side_effect=db.save)),
        patch("app.services.expansion_manager.get_rolling_trade_stats",
              new=AsyncMock(return_value=sparse_stats)),
        patch.multiple("app.services.expansion_manager.settings", **_EXPANSION_SETTINGS),
    ):
        result = await expansion_manager.after_trade(account, is_win=True)

    assert result.active is False, "Should stay defensive with fewer than 30 trades"


@pytest.mark.asyncio
async def test_phase1_does_not_activate_with_low_win_rate():
    """52% win rate is below the 60% gate — must stay defensive."""
    db = _StatefulExpansion()
    account = _account()
    weak_stats = {
        "total": 30, "wins": 16, "losses": 14,
        "win_rate": 0.52, "max_dd_pct": 1.0, "pnls": [],
    }

    with (
        patch("app.services.expansion_manager.load_expansion_state",
              new=AsyncMock(side_effect=db.load)),
        patch("app.services.expansion_manager.save_expansion_state",
              new=AsyncMock(side_effect=db.save)),
        patch("app.services.expansion_manager.get_rolling_trade_stats",
              new=AsyncMock(return_value=weak_stats)),
        patch.multiple("app.services.expansion_manager.settings", **_EXPANSION_SETTINGS),
    ):
        result = await expansion_manager.after_trade(account, is_win=True)

    assert result.active is False


# ---------------------------------------------------------------------------
# Phase 2 — Two consecutive losses inside expansion trigger exit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase2_first_loss_does_not_exit():
    """
    First loss in expansion increments counter to 1 but does NOT exit yet
    (threshold is 3).
    """
    db = _StatefulExpansion()
    # Start already in expansion
    db._state = ExpansionState(
        active=True, trades_in_window=5, consecutive_losses=0,
        start_equity=10_500.0,
    )
    account = _account(equity=10_300.0, peak=10_500.0)  # small loss, within DD

    with (
        patch("app.services.expansion_manager.load_expansion_state",
              new=AsyncMock(side_effect=db.load)),
        patch("app.services.expansion_manager.save_expansion_state",
              new=AsyncMock(side_effect=db.save)),
        patch.multiple("app.services.expansion_manager.settings", **_EXPANSION_SETTINGS),
    ):
        result = await expansion_manager.after_trade(account, is_win=False)

    assert result.active is True,  "One loss must NOT exit expansion"
    assert result.consecutive_losses == 1
    assert result.trades_in_window == 6


@pytest.mark.asyncio
async def test_phase2_second_loss_does_not_exit():
    """
    Second consecutive loss increments counter to 2 but does NOT exit yet
    (threshold is 3).
    """
    db = _StatefulExpansion()
    db._state = ExpansionState(
        active=True, trades_in_window=6, consecutive_losses=1,
        start_equity=10_500.0,
    )
    account = _account(equity=10_380.0, peak=10_500.0)  # small loss, within DD

    with (
        patch("app.services.expansion_manager.load_expansion_state",
              new=AsyncMock(side_effect=db.load)),
        patch("app.services.expansion_manager.save_expansion_state",
              new=AsyncMock(side_effect=db.save)),
        patch.multiple("app.services.expansion_manager.settings", **_EXPANSION_SETTINGS),
    ):
        result = await expansion_manager.after_trade(account, is_win=False)

    assert result.active is True, "Two consecutive losses must NOT exit (threshold=3)"
    assert result.consecutive_losses == 2
    assert result.trades_in_window == 7


@pytest.mark.asyncio
async def test_phase2_third_loss_exits_expansion():
    """
    Third consecutive loss hits the exit threshold (3) → Expansion Mode deactivates.
    """
    db = _StatefulExpansion()
    # Already have 2 consecutive losses — one more triggers exit
    db._state = ExpansionState(
        active=True, trades_in_window=7, consecutive_losses=2,
        start_equity=10_500.0,
    )
    account = _account(equity=10_270.0, peak=10_500.0)  # third small loss

    with (
        patch("app.services.expansion_manager.load_expansion_state",
              new=AsyncMock(side_effect=db.load)),
        patch("app.services.expansion_manager.save_expansion_state",
              new=AsyncMock(side_effect=db.save)),
        patch.multiple("app.services.expansion_manager.settings", **_EXPANSION_SETTINGS),
    ):
        result = await expansion_manager.after_trade(account, is_win=False)

    assert result.active is False, "Three consecutive losses must exit expansion"
    assert "consecutive_losses" in (result.exit_reason or "")
    assert result.trades_in_window == 8   # final count preserved for audit log


# ---------------------------------------------------------------------------
# Phase 3 — Defensive fallback confirmed after exit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase3_risk_returns_to_defensive_after_exit():
    """
    After expansion exits, the very next trade sizing must use 0.5% (defensive),
    not 0.9% (expansion).
    """
    with patch.multiple("app.services.risk_manager.settings", **_EXPANSION_SETTINGS):
        sizing = risk_manager.compute_position_pnl(
            equity=10_210.0,
            consecutive_losses=0,    # fresh start after win-streak reset
            expansion_active=False,  # expansion just exited
        )

    assert sizing["mode"] == "defensive"
    assert sizing["risk_pct"] == pytest.approx(0.5)
    assert sizing["loss"] == pytest.approx(-51.05)   # 10_210 * 0.5%
    assert sizing["win"] == pytest.approx(91.89)     # loss * 1.8


@pytest.mark.asyncio
async def test_phase3_after_trade_does_not_reactivate_immediately():
    """
    Expansion was just revoked. The very next after_trade (even with a win)
    must NOT re-activate because rolling stats haven't improved yet.

    Low win-rate stats simulate the degraded window post-deactivation.
    """
    db = _StatefulExpansion()
    db._state = ExpansionState(active=False, exit_reason="consecutive_losses (3)")
    account = _account(equity=10_300.0, peak=10_500.0)  # not at peak
    # Rolling window now shows degraded stats due to recent losses
    degraded_stats = {
        "total": 30, "wins": 17, "losses": 13,
        "win_rate": 0.567, "max_dd_pct": 4.1, "pnls": [],
    }

    with (
        patch("app.services.expansion_manager.load_expansion_state",
              new=AsyncMock(side_effect=db.load)),
        patch("app.services.expansion_manager.save_expansion_state",
              new=AsyncMock(side_effect=db.save)),
        patch("app.services.expansion_manager.get_rolling_trade_stats",
              new=AsyncMock(return_value=degraded_stats)),
        patch.multiple("app.services.expansion_manager.settings", **_EXPANSION_SETTINGS),
    ):
        result = await expansion_manager.after_trade(account, is_win=True)

    assert result.active is False
    assert result.exit_reason == "consecutive_losses (3)"  # reason preserved


# ---------------------------------------------------------------------------
# Full lifecycle in one test (narrative / smoke)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_lifecycle_activate_then_exit():
    """
    Runs the complete Mode C lifecycle in a single test:

    1. Start defensive → verify defensive risk
    2. Present 30-trade qualifying window → expansion activates
    3. Inside expansion: three consecutive losses → auto-exit
    4. Back to defensive → verify defensive risk
    """
    db = _StatefulExpansion()  # starts with active=False
    equity = 10_500.0
    account_at_peak = _account(equity=equity, peak=equity)

    settings_patch = patch.multiple(
        "app.services.expansion_manager.settings", **_EXPANSION_SETTINGS
    )
    risk_settings_patch = patch.multiple(
        "app.services.risk_manager.settings", **_EXPANSION_SETTINGS
    )
    load_mock = patch("app.services.expansion_manager.load_expansion_state",
                      new=AsyncMock(side_effect=db.load))
    save_mock = patch("app.services.expansion_manager.save_expansion_state",
                      new=AsyncMock(side_effect=db.save))
    stats_mock = patch("app.services.expansion_manager.get_rolling_trade_stats",
                       new=AsyncMock(return_value=_good_stats(30)))

    with settings_patch, risk_settings_patch, load_mock, save_mock, stats_mock:

        # ── Step 1: confirm defensive pricing before anything ──────────────
        sizing_before = risk_manager.compute_position_pnl(
            equity=equity, consecutive_losses=0, expansion_active=False
        )
        assert sizing_before["mode"] == "defensive"
        assert sizing_before["risk_pct"] == pytest.approx(0.5)

        # ── Step 2: trade 30 fires → all gates pass → ACTIVATE ────────────
        state_after_activate = await expansion_manager.after_trade(
            account_at_peak, is_win=True
        )
        assert state_after_activate.active is True
        assert state_after_activate.start_equity == pytest.approx(equity)

        sizing_expansion = risk_manager.compute_position_pnl(
            equity=equity, consecutive_losses=0, expansion_active=True
        )
        assert sizing_expansion["mode"] == "expansion"
        assert sizing_expansion["risk_pct"] == pytest.approx(0.9)

        # ── Step 3: first loss — consecutive_losses=1, still active ─────────
        db._state = state_after_activate   # carry state forward
        account_after_loss1 = _account(equity=10_430.0, peak=10_500.0)
        state_after_loss1 = await expansion_manager.after_trade(
            account_after_loss1, is_win=False
        )
        assert state_after_loss1.active is True, (
            f"After 1 loss expansion must still be active, "
            f"got exit_reason={state_after_loss1.exit_reason!r}"
        )
        assert state_after_loss1.consecutive_losses == 1

        # ── Step 4: second loss — consecutive_losses=2, still active (thr=3) ─
        db._state = state_after_loss1
        account_after_loss2 = _account(equity=10_350.0, peak=10_500.0)
        state_after_loss2 = await expansion_manager.after_trade(
            account_after_loss2, is_win=False
        )
        assert state_after_loss2.active is True, (
            f"After 2 losses expansion must still be active (threshold=3), "
            f"got exit_reason={state_after_loss2.exit_reason!r}"
        )
        assert state_after_loss2.consecutive_losses == 2

        # ── Step 5: third loss — consecutive_losses=3 hits threshold → EXIT ──
        #   equity=10_240 → 2.5% DD from 10_500 start (< 3% DD gate)
        db._state = state_after_loss2
        account_after_loss3 = _account(equity=10_240.0, peak=10_500.0)
        state_final = await expansion_manager.after_trade(
            account_after_loss3, is_win=False
        )
        assert state_final.active is False
        assert "consecutive_losses" in (state_final.exit_reason or "")

        # ── Step 6: confirm risk is back to defensive immediately ──────────
        sizing_after = risk_manager.compute_position_pnl(
            equity=10_240.0, consecutive_losses=0, expansion_active=False
        )
        assert sizing_after["mode"] == "defensive"
        assert sizing_after["risk_pct"] == pytest.approx(0.5)
        # Smaller equity → smaller absolute risk (proving equity-scaling too)
        assert sizing_after["loss"] > sizing_before["loss"]  # less negative = smaller
