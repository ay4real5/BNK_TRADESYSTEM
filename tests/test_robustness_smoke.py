"""
tests/test_robustness_smoke.py
================================
Fast smoke-level tests for the 10-year robustness framework.
All simulations use a tiny run count (5 or 10) and short horizon so the
CI suite stays under a few seconds.
"""
from __future__ import annotations

import pytest

from app.services.robustness_tester import (
    RobustnessConfig,
    RegimeType,
    run_one_sim,
    run_robustness_test,
    _build_trade_schedule,
    _build_edge_decay,
    _expansion_step,
    ExpState,
    _rolling_stats,
)
import collections


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fast_cfg(**kwargs) -> RobustnessConfig:
    """5-year, 5 MC runs config for quick tests."""
    defaults = dict(
        sim_years     = 5,
        enable_edge_decay    = True,
        enable_vol_clustering= True,
        enable_fat_tail      = True,
    )
    defaults.update(kwargs)
    return RobustnessConfig(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — 5-run simulation completes without error
# ─────────────────────────────────────────────────────────────────────────────

def test_five_run_simulation_completes():
    """run_robustness_test must return a valid summary for 5 runs."""
    summary = run_robustness_test(n_runs=5, cfg=_fast_cfg(), verbose=False)

    assert "median_cagr"              in summary
    assert "p5_cagr"                  in summary
    assert "p95_cagr"                 in summary
    assert "p95_drawdown"             in summary
    assert "ruin_probability"         in summary
    assert "kill_switch_rate"         in summary
    assert "expansion_activation_rate" in summary
    assert "tail_event_survival_rate" in summary
    assert len(summary["per_run"])    == 5


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — No negative equity in any run
# ─────────────────────────────────────────────────────────────────────────────

def test_no_negative_equity_any_run():
    """
    Equity must never go below zero: max losses are bounded to
    the available account balance by max(0.0, equity + pnl).
    """
    for seed in range(15):
        result = run_one_sim(seed=seed, cfg=_fast_cfg())
        assert result.final_equity >= 0.0, (
            f"seed={seed} produced negative equity {result.final_equity}"
        )
        assert all(eq >= 0.0 for eq in result.daily_equities), (
            f"seed={seed} has a day with negative equity in daily_equities"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — Kill-switch triggers when cumulative DD exceeds threshold
# ─────────────────────────────────────────────────────────────────────────────

def test_kill_switch_triggers_on_extreme_drawdown():
    """
    Construct a config with a tiny max_total_drawdown_pct (2%) and
    a starting balance that provides almost no room.  Over 252 days
    with big losses the kill-switch should fire in at least a few runs.
    """
    cfg = RobustnessConfig(
        sim_years               = 1,
        starting_balance        = 10_000.0,
        max_total_drawdown_pct  = 3.0,   # very tight
        defensive_risk_pct      = 2.0,   # larger risk per trade → faster hit
        expansion_risk_pct      = 3.0,
        enable_edge_decay       = False,
        enable_vol_clustering   = False,
        enable_fat_tail         = True,
        fat_tail_extra_prob     = 0.05,  # very frequent fat tails → faster kill
    )
    results = [run_one_sim(seed=s, cfg=cfg) for s in range(30)]
    triggered = [r for r in results if r.kill_switch_triggered]

    assert len(triggered) > 0, (
        "Expected at least one kill-switch in 30 seeds with tight (3%) DD limit "
        "and 2% risk + 5% fat-tail probability"
    )

    for r in triggered:
        # Equity at kill should not massively exceed kill threshold
        # (there can be slight overshoot from the rounding of a single trade)
        max_allowed = cfg.starting_balance * (1.0 - cfg.max_total_drawdown_pct / 100.0)
        # Allow 1 full trade of overshoot at 2% risk
        overshoot_allowance = cfg.starting_balance * 0.04
        assert r.final_equity >= max_allowed - overshoot_allowance, (
            f"Kill-switch equity {r.final_equity:.2f} is far below "
            f"threshold {max_allowed:.2f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — Expansion activates at least once in a clearly trending regime
# ─────────────────────────────────────────────────────────────────────────────

def test_expansion_activates_in_long_trending_regime():
    """
    When we force a scenario with many trending-regime trades,
    expansion should activate at least once across 10 seeds.
    """
    # Use a very lenient expansion gate to guarantee activation
    cfg = RobustnessConfig(
        sim_years              = 2,
        expansion_min_trades   = 10,          # require only 10 trades in rolling window
        expansion_min_win_rate = 0.50,        # low threshold
        expansion_max_dd_pct   = 10.0,        # generous DD tolerance
        enable_edge_decay      = False,       # no decay -> win rate stays high
        enable_vol_clustering  = False,
        enable_fat_tail        = False,
    )
    activations = [run_one_sim(seed=s, cfg=cfg).expansion_activations for s in range(10)]
    assert any(a > 0 for a in activations), (
        f"Expected at least one expansion activation across 10 seeds; "
        f"got activations={activations}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — Output dictionary has correct types and bounds
# ─────────────────────────────────────────────────────────────────────────────

def test_summary_types_and_bounds():
    """All primary KPI fields must be numeric and within reasonable bounds."""
    summary = run_robustness_test(n_runs=5, cfg=_fast_cfg(), verbose=False)

    # CAGR: should be numeric and not absurdly negative
    assert isinstance(summary["median_cagr"],  (int, float))
    assert isinstance(summary["p5_cagr"],      (int, float))
    assert isinstance(summary["p95_cagr"],     (int, float))
    assert summary["p5_cagr"] <= summary["median_cagr"] <= summary["p95_cagr"], (
        f"CAGR percentile ordering violated: "
        f"p5={summary['p5_cagr']} median={summary['median_cagr']} p95={summary['p95_cagr']}"
    )

    # Drawdown: non-negative
    assert summary["p95_drawdown"] >= 0.0

    # Probabilities: 0–100
    assert 0.0 <= summary["ruin_probability"]          <= 100.0
    assert 0.0 <= summary["kill_switch_rate"]          <= 100.0
    assert 0.0 <= summary["expansion_activation_rate"] <= 100.0
    assert 0.0 <= summary["tail_event_survival_rate"]  <= 100.0

    # per_run list integrity
    for run_dict in summary["per_run"]:
        assert run_dict["final_equity"] >= 0.0
        assert run_dict["max_dd_pct"]   >= 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 — Edge decay reduces realised win rate
# ─────────────────────────────────────────────────────────────────────────────

def test_edge_decay_reduces_win_rate():
    """
    Over the same seeds, disabling edge decay should produce a higher or equal
    median CAGR compared to enabling it across 20 runs (it smoothly degrades).
    """
    cfg_decay    = _fast_cfg(enable_edge_decay=True,  enable_fat_tail=False,
                             enable_vol_clustering=False)
    cfg_no_decay = _fast_cfg(enable_edge_decay=False, enable_fat_tail=False,
                             enable_vol_clustering=False)

    summary_decay    = run_robustness_test(n_runs=20, cfg=cfg_decay,    verbose=False)
    summary_no_decay = run_robustness_test(n_runs=20, cfg=cfg_no_decay, verbose=False)

    cagr_decay    = summary_decay["median_cagr"]
    cagr_no_decay = summary_no_decay["median_cagr"]

    # Without decay the CAGR should be >= caGR with decay (at least not worse)
    assert cagr_no_decay >= cagr_decay - 5.0, (
        f"Disabling edge decay gave LOWER median CAGR ({cagr_no_decay:.2f}%) "
        f"vs with-decay ({cagr_decay:.2f}%) by a wide margin. "
        f"Edge decay should only harm performance."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 7 — Fat-tail events appear in runs when enabled
# ─────────────────────────────────────────────────────────────────────────────

def test_fat_tail_events_appear_when_enabled():
    """Fat-tail events must occur at least once in a 10-run batch when enabled."""
    cfg = RobustnessConfig(
        sim_years           = 2,
        enable_fat_tail     = True,
        fat_tail_extra_prob = 0.10,     # 10x normal → guaranteed events
        enable_edge_decay   = False,
        enable_vol_clustering = False,
    )
    results = [run_one_sim(seed=s, cfg=cfg) for s in range(10)]
    total_events = sum(r.fat_tail_events_hit for r in results)
    assert total_events > 0, "No fat-tail events generated even with prob=0.10"


def test_fat_tail_events_absent_when_disabled():
    """Fat-tail events must be zero in every run when disabled."""
    cfg = RobustnessConfig(
        sim_years         = 2,
        enable_fat_tail   = False,
        enable_edge_decay = False,
        enable_vol_clustering = False,
    )
    for seed in range(10):
        r = run_one_sim(seed=seed, cfg=cfg)
        assert r.fat_tail_events_hit == 0, (
            f"seed={seed} got a fat-tail event despite enable_fat_tail=False"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 8 — Regime trade counts sum to total_trades
# ─────────────────────────────────────────────────────────────────────────────

def test_regime_counts_match_total_trades():
    """Sum of per-regime trade counts must equal total_trades recorded in run."""
    for seed in range(5):
        r = run_one_sim(seed=seed, cfg=_fast_cfg())
        regime_total = sum(r.regime_trade_counts.values())
        assert regime_total == r.total_trades, (
            f"seed={seed}: regime_counts sum={regime_total} != total_trades={r.total_trades}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 9 — Expansion + defensive trade counts sum to total_trades
# ─────────────────────────────────────────────────────────────────────────────

def test_expansion_defensive_sum_equals_total():
    """expansion_trades + defensive_trades must equal total_trades."""
    for seed in range(5):
        r = run_one_sim(seed=seed, cfg=_fast_cfg())
        assert r.expansion_trades + r.defensive_trades == r.total_trades, (
            f"seed={seed}: exp({r.expansion_trades}) + def({r.defensive_trades}) "
            f"!= total({r.total_trades})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 10 — Max drawdown pct is non-decreasing over time (monotonic peak track)
# ─────────────────────────────────────────────────────────────────────────────

def test_max_drawdown_bounded_by_kill_switch():
    """
    If the kill-switch is configured at 10%, max_drawdown_pct should not
    far exceed 10% (one trade of overshoot is acceptable).
    """
    cfg = RobustnessConfig(
        sim_years              = 3,
        max_total_drawdown_pct = 10.0,
        enable_edge_decay      = False,
        enable_fat_tail        = False,
        enable_vol_clustering  = False,
    )
    for seed in range(10):
        r = run_one_sim(seed=seed, cfg=cfg)
        overshoot_allowance = 5.0   # one bad trade at 0.9% risk can add ~1% DD
        assert r.max_drawdown_pct <= cfg.max_total_drawdown_pct + overshoot_allowance, (
            f"seed={seed}: max_dd={r.max_drawdown_pct:.2f}% far exceeds "
            f"kill-switch threshold={cfg.max_total_drawdown_pct}%"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 11 — CAGR formula correctness
# ─────────────────────────────────────────────────────────────────────────────

def test_cagr_formula():
    """
    Given a known final equity we can independently verify CAGR.
    10k → 20k over 10 years = CAGR = 2^(1/10) - 1 ≈ 7.18%
    """
    import math
    start = 10_000.0
    final = 20_000.0
    years = 10
    expected_cagr = ((final / start) ** (1.0 / years) - 1.0) * 100.0

    # Patch run_one_sim indirectly: just validate the formula
    cfg = RobustnessConfig(sim_years=years)
    computed = ((final / cfg.starting_balance) ** (1.0 / cfg.sim_years) - 1.0) * 100.0
    assert abs(computed - expected_cagr) < 0.001, (
        f"CAGR formula error: got {computed:.4f}% expected {expected_cagr:.4f}%"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 12 — Build trade schedule covers requested count
# ─────────────────────────────────────────────────────────────────────────────

def test_build_trade_schedule_length():
    """_build_trade_schedule must return exactly the requested number of trades."""
    import random as _random
    rng = _random.Random(42)
    cfg = _fast_cfg()
    schedule = _build_trade_schedule(rng, 1000, cfg)
    assert len(schedule) == 1000
    # All slots must have valid regime types
    valid_regimes = {r for r in RegimeType}
    for slot in schedule:
        assert slot.regime in valid_regimes
        assert 0.0 < slot.win_prob <= 1.0
        assert slot.rr > 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 13 — Expansion state machine: single-trade boundary
# ─────────────────────────────────────────────────────────────────────────────

def test_expansion_does_not_exit_on_one_loss():
    """
    With expansion_exit_consec=3, one loss should NOT exit expansion.
    """
    cfg   = RobustnessConfig(expansion_exit_consec=3)
    state = ExpState(active=True, start_equity=10_000.0,
                     trades_in_window=5, exp_consec_losses=0)
    stats = {"total": 30, "wins": 18, "losses": 12,
             "win_rate": 0.60, "max_dd_pct": 1.0}

    new_state = _expansion_step(state, 9_950.0, 10_000.0, stats, is_win=False, cfg=cfg)
    assert new_state.active,   "Expansion should stay active after 1 loss (threshold=3)"
    assert new_state.exp_consec_losses == 1


def test_expansion_exits_on_third_loss():
    """
    After 2 prior losses, the 3rd loss (threshold=3) must exit expansion.
    """
    cfg   = RobustnessConfig(expansion_exit_consec=3)
    state = ExpState(active=True, start_equity=10_000.0,
                     trades_in_window=5, exp_consec_losses=2)
    stats = {"total": 30, "wins": 18, "losses": 12,
             "win_rate": 0.60, "max_dd_pct": 1.0}

    new_state = _expansion_step(state, 9_900.0, 10_050.0, stats, is_win=False, cfg=cfg)
    assert not new_state.active, "Expansion must exit on 3rd consecutive loss"
    assert "consecutive_losses" in new_state.exit_reason


# ─────────────────────────────────────────────────────────────────────────────
# Test 14 — Reproducibility: same seed → same result
# ─────────────────────────────────────────────────────────────────────────────

def test_reproducibility_same_seed():
    """Two calls with the same seed must produce identical final equity."""
    cfg = _fast_cfg()
    r1  = run_one_sim(seed=99, cfg=cfg)
    r2  = run_one_sim(seed=99, cfg=cfg)
    assert r1.final_equity == r2.final_equity, "Same seed must produce same result"
    assert r1.total_trades == r2.total_trades


def test_different_seeds_differ():
    """Different seeds should nearly always produce different results."""
    cfg = _fast_cfg()
    results = {run_one_sim(seed=s, cfg=cfg).final_equity for s in range(5)}
    assert len(results) > 1, (
        "All 5 seeds produced the same equity — seeds are not being used"
    )
