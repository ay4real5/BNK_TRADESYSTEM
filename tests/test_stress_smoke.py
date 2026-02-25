"""
Stress-test smoke suite — runs 20 sims quickly and asserts structural
invariants about the simulation engine.

Run:
    python -m pytest tests/test_stress_smoke.py -v
"""

from __future__ import annotations

import sys
import os

# Allow importing from scripts/ without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.stress_test import (
    SimConfig,
    ExpState,
    _build_regime_schedule,
    _compute_rolling_stats,
    _expansion_after_trade,
    run_one_simulation,
    run_stress_test,
)

# ── shared config ─────────────────────────────────────────────────────────────

SMOKE_CFG  = SimConfig(starting_balance=10_000.0, sim_days=126)  # ~6 months
N_SMOKE    = 20


# ──────────────────────────────────────────────────────────────────────────────
# 1. Equity safety — never goes negative
# ──────────────────────────────────────────────────────────────────────────────

def test_equity_never_goes_negative():
    """
    No matter how bad the regime sequence, equity must never be driven below $0.
    The simulation clips equity at 0.0 and the kill-switch should halt it first,
    but in either case the reported final equity must be >= 0.
    """
    for i in range(N_SMOKE):
        result = run_one_simulation(seed=10_000 + i, cfg=SMOKE_CFG)
        assert result.final_equity >= 0.0, (
            f"Sim seed={result.seed}: final equity ${result.final_equity:.2f} is negative"
        )
        # Also all daily snapshots
        for day_idx, eq in enumerate(result.daily_equities):
            assert eq >= 0.0, (
                f"Sim seed={result.seed}: day {day_idx} equity ${eq:.2f} < 0"
            )


# ──────────────────────────────────────────────────────────────────────────────
# 2. Kill-switch fires only when the configured threshold is breached
# ──────────────────────────────────────────────────────────────────────────────

def test_kill_switch_only_when_threshold_breached():
    """
    If a simulation reports kill_switch_triggered=True, the equity at the
    moment of kill must be at or below (peak * (1 - max_total_drawdown_pct/100)).
    We run with a lower drawdown threshold (5%) to force more kill events.
    """
    cfg_tight = SimConfig(
        starting_balance=10_000.0,
        sim_days=252,
        max_total_drawdown_pct=5.0,   # tighter threshold to trigger kills in smoke run
        defensive_risk_pct=1.5,        # higher risk to accelerate kill probability
        expansion_risk_pct=2.5,
    )
    found_kills = 0
    for i in range(N_SMOKE):
        result = run_one_simulation(seed=20_000 + i, cfg=cfg_tight)
        if result.kill_switch_triggered:
            found_kills += 1
            assert result.equity_at_kill is not None
            assert result.kill_switch_pct_below_peak is not None
            # Kill-switch must have fired at or beyond the threshold
            assert result.kill_switch_pct_below_peak >= cfg_tight.max_total_drawdown_pct * 0.98, (
                f"Kill-switch at {result.kill_switch_pct_below_peak:.2f}% but threshold "
                f"is {cfg_tight.max_total_drawdown_pct}%"
            )
            # Equity at kill must be positive
            assert result.equity_at_kill >= 0.0


def test_no_kill_switch_without_breach():
    """
    With a very generous 50% kill-switch threshold, no simulation in a
    standard 6-month run should ever trigger the kill-switch.
    """
    cfg_safe = SimConfig(
        starting_balance=10_000.0,
        sim_days=126,
        max_total_drawdown_pct=50.0,   # virtually impossible to breach
    )
    for i in range(N_SMOKE):
        result = run_one_simulation(seed=30_000 + i, cfg=cfg_safe)
        assert not result.kill_switch_triggered, (
            f"Sim seed={result.seed} triggered kill-switch with a 50% threshold"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 3. Expansion invariants — state machine correctness
# ──────────────────────────────────────────────────────────────────────────────

def _base_cfg() -> SimConfig:
    return SimConfig()


def test_expansion_exits_on_third_consecutive_loss():
    """
    Starting with 2 consecutive losses already logged inside expansion,
    a third loss must trigger an exit with reason containing 'consecutive_losses'.
    """
    cfg = _base_cfg()
    state = ExpState(active=True, start_equity=10_000.0, exp_consecutive_losses=2)
    stats = {"total": 35, "wins": 25, "losses": 10, "win_rate": 0.714, "max_dd_pct": 1.0}

    new_state = _expansion_after_trade(
        state=state,
        account_equity=9_950.0,     # small loss, not yet at DD gate
        peak_equity=10_000.0,
        rolling_stats=stats,
        is_win=False,
        atr_spike=False,
        cfg=cfg,
    )
    assert new_state.active is False
    assert "consecutive_losses" in new_state.exit_reason


def test_expansion_does_not_exit_after_single_loss():
    """First loss should increment counter but NOT exit (threshold is 3)."""
    cfg = _base_cfg()
    state = ExpState(active=True, start_equity=10_000.0, exp_consecutive_losses=0)
    stats = {"total": 35, "wins": 25, "losses": 10, "win_rate": 0.714, "max_dd_pct": 1.0}

    new_state = _expansion_after_trade(
        state=state,
        account_equity=9_960.0,
        peak_equity=10_000.0,
        rolling_stats=stats,
        is_win=False,
        atr_spike=False,
        cfg=cfg,
    )
    assert new_state.active is True
    assert new_state.exp_consecutive_losses == 1


def test_expansion_consecutive_loss_resets_on_win():
    """A win inside expansion must reset the consecutive loss counter to 0."""
    cfg = _base_cfg()
    state = ExpState(active=True, start_equity=10_000.0, exp_consecutive_losses=1)
    stats = {"total": 35, "wins": 26, "losses": 9, "win_rate": 0.743, "max_dd_pct": 0.8}

    new_state = _expansion_after_trade(
        state=state,
        account_equity=10_100.0,
        peak_equity=10_100.0,
        rolling_stats=stats,
        is_win=True,
        atr_spike=False,
        cfg=cfg,
    )
    assert new_state.active is True
    assert new_state.exp_consecutive_losses == 0


def test_expansion_exits_on_window_exhausted():
    """With trades_in_window=19, the next trade (→ 20) must close the window."""
    cfg = _base_cfg()   # expansion_max_trades=20
    state = ExpState(active=True, start_equity=10_000.0, trades_in_window=19)
    stats = {"total": 35, "wins": 25, "losses": 10, "win_rate": 0.714, "max_dd_pct": 1.0}

    new_state = _expansion_after_trade(
        state=state,
        account_equity=10_200.0,
        peak_equity=10_200.0,
        rolling_stats=stats,
        is_win=True,
        atr_spike=False,
        cfg=cfg,
    )
    assert new_state.active is False
    assert "window_exhausted" in new_state.exit_reason
    assert new_state.trades_in_window == 20


def test_expansion_exits_on_dd_from_start():
    """
    3.5% drawdown from expansion start_equity (10_000) → (9_650) exceeds
    the 3% exit gate → must exit.
    """
    cfg = _base_cfg()
    state = ExpState(active=True, start_equity=10_000.0, exp_consecutive_losses=0)
    stats = {"total": 35, "wins": 23, "losses": 12, "win_rate": 0.657, "max_dd_pct": 1.5}

    new_state = _expansion_after_trade(
        state=state,
        account_equity=9_640.0,    # 3.6% below 10_000
        peak_equity=10_000.0,
        rolling_stats=stats,
        is_win=False,
        atr_spike=False,
        cfg=cfg,
    )
    assert new_state.active is False
    assert "drawdown_from_start" in new_state.exit_reason


def test_expansion_exits_on_atr_spike():
    """ATR spike while in expansion must trigger immediate exit."""
    cfg = _base_cfg()
    state = ExpState(active=True, start_equity=10_000.0, exp_consecutive_losses=0)
    stats = {"total": 35, "wins": 25, "losses": 10, "win_rate": 0.714, "max_dd_pct": 1.0}

    new_state = _expansion_after_trade(
        state=state,
        account_equity=10_100.0,
        peak_equity=10_100.0,
        rolling_stats=stats,
        is_win=True,
        atr_spike=True,    # ← spike
        cfg=cfg,
    )
    assert new_state.active is False
    assert "atr_spike" in new_state.exit_reason


def test_expansion_does_not_activate_below_min_trades():
    """Fewer than 30 trades in window → never activates."""
    cfg = _base_cfg()
    state = ExpState(active=False)
    stats = {"total": 15, "wins": 12, "losses": 3, "win_rate": 0.80, "max_dd_pct": 0.5}

    new_state = _expansion_after_trade(
        state=state,
        account_equity=10_500.0,
        peak_equity=10_500.0,
        rolling_stats=stats,
        is_win=True,
        atr_spike=False,
        cfg=cfg,
    )
    assert new_state.active is False


def test_expansion_does_not_activate_without_equity_at_peak():
    """Even if stats are great, equity must be at peak (gate 3)."""
    cfg = _base_cfg()
    state = ExpState(active=False)
    stats = {"total": 35, "wins": 25, "losses": 10, "win_rate": 0.714, "max_dd_pct": 1.0}

    new_state = _expansion_after_trade(
        state=state,
        account_equity=9_800.0,   # below peak
        peak_equity=10_500.0,
        rolling_stats=stats,
        is_win=True,
        atr_spike=False,
        cfg=cfg,
    )
    assert new_state.active is False


# ──────────────────────────────────────────────────────────────────────────────
# 4. Expansion never STAYS active if any exit trigger is present
#    (property-based style: checks every simulated trade event)
# ──────────────────────────────────────────────────────────────────────────────

def test_expansion_never_stays_active_when_exit_asserted():
    """
    Starting from various initial consecutive-loss counts inside expansion,
    driving losses until the threshold (3) is reached must always produce
    an inactive state.
    """
    # We'll test this by directly cycling through specific scenarios
    cfg = _base_cfg()
    # Start active, throw 2 consecutive losses — must be inactive by end
    for start_consec in range(0, 2):
        state = ExpState(active=True, start_equity=10_000.0, exp_consecutive_losses=start_consec)
        stats = {"total": 35, "wins": 25, "losses": 10, "win_rate": 0.714, "max_dd_pct": 1.0}

        # Drive consecutive_losses to exactly expand_exit_consec_losses
        for _ in range(cfg.expansion_exit_consec_losses - start_consec):
            state = _expansion_after_trade(
                state=state,
                account_equity=9_990.0,    # tiny loss, within DD gate
                peak_equity=10_000.0,
                rolling_stats=stats,
                is_win=False,
                atr_spike=False,
                cfg=cfg,
            )

        # By now we've hit the threshold — must be inactive
        assert state.active is False, (
            f"Expansion still active after {cfg.expansion_exit_consec_losses} "
            f"consecutive losses (started at {start_consec})"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 5. Aggregate smoke — run 20 sims, check structural properties
# ──────────────────────────────────────────────────────────────────────────────

def test_aggregate_smoke_structural_properties():
    """
    Run full stress harness with N=20, check:
    - all key sections present in summary
    - equity distribution values are monotone (min ≤ p25 ≤ median ≤ p75 ≤ max)
    - pct values are within [0, 100]
    - lock pcts are within [0, 100]
    - expansion data is internally consistent
    """
    summary = run_stress_test(n_sims=N_SMOKE, cfg=SMOKE_CFG, base_seed=99_000, verbose=False)

    # Required keys present
    for section in ["meta", "equity_distribution", "drawdown_distribution",
                    "locks", "kill_switch", "expansion", "trades"]:
        assert section in summary, f"Missing section: {section}"

    # Equity distribution must be monotone
    ed = summary["equity_distribution"]
    assert ed["min"] <= ed["p5"]      <= ed["p25"] <= ed["median"], \
        f"Equity distribution not monotone (lower half): {ed}"
    assert ed["median"] <= ed["p75"] <= ed["p95"]  <= ed["max"], \
        f"Equity distribution not monotone (upper half): {ed}"

    # Percentages in range
    assert 0 <= summary["equity_distribution"]["pct_profitable"] <= 100
    assert 0 <= summary["locks"]["pct_runs_hitting_daily_lock"]  <= 100
    assert 0 <= summary["locks"]["pct_runs_hitting_kill_switch"] <= 100
    assert 0 <= summary["expansion"]["avg_pct_trades_in_expansion"] <= 100

    # Defensive + expansion must sum to ~100%
    total_pct = (
        summary["expansion"]["avg_pct_trades_in_expansion"] +
        summary["expansion"]["avg_pct_trades_in_defensive"]
    )
    assert abs(total_pct - 100.0) < 0.01, f"defensive + expansion != 100%: {total_pct}"

    # DD distribution monotone
    dd = summary["drawdown_distribution"]
    assert dd["min"] <= dd["p50"] <= dd["p90"] <= dd["p95"] <= dd["p99"] <= dd["max"], \
        f"DD distribution not monotone: {dd}"

    # All min DDs must be non-negative
    assert dd["min"] >= 0.0

    # per_sim count
    assert len(summary["per_sim"]) == N_SMOKE


# ──────────────────────────────────────────────────────────────────────────────
# 6. Rolling stats correctness
# ──────────────────────────────────────────────────────────────────────────────

def test_rolling_stats_pure_wins():
    from collections import deque
    pnls = deque([50.0] * 30, maxlen=30)
    stats = _compute_rolling_stats(pnls)
    assert stats["total"] == 30
    assert stats["wins"] == 30
    assert stats["losses"] == 0
    assert stats["win_rate"] == 1.0
    assert stats["max_dd_pct"] == 0.0   # monotonically rising, no drawdown


def test_rolling_stats_pure_losses():
    from collections import deque
    pnls = deque([-50.0] * 30, maxlen=30)
    stats = _compute_rolling_stats(pnls)
    assert stats["total"] == 30
    assert stats["wins"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["max_dd_pct"] > 0.0    # losses create drawdown


def test_rolling_stats_empty():
    from collections import deque
    stats = _compute_rolling_stats(deque())
    assert stats["total"] == 0
    assert stats["win_rate"] == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# 7. Regime schedule is well-formed
# ──────────────────────────────────────────────────────────────────────────────

def test_regime_schedule_length_and_valid_values():
    import random
    rng = random.Random(42)
    n = 200
    schedule = _build_regime_schedule(rng, n)
    assert len(schedule) == n

    valid_regimes = {"good", "chop", "bad"}
    for regime, win_prob, atr_spike in schedule:
        assert regime in valid_regimes, f"Unknown regime: {regime}"
        assert 0.0 < win_prob < 1.0, f"Win prob out of range: {win_prob}"
        assert isinstance(atr_spike, bool)


def test_regime_schedule_contains_all_types():
    """With enough trades, all three regime types must appear."""
    import random
    rng = random.Random(777)
    schedule = _build_regime_schedule(rng, 5_000)
    regimes_seen = {r for r, _, _ in schedule}
    assert "good"  in regimes_seen
    assert "chop"  in regimes_seen
    assert "bad"   in regimes_seen
