#!/usr/bin/env python3
"""
BNK_TRADESYSTEM — Stress Test Harness
======================================

Simulates N independent months of trading using the same Mode C logic
(expansion gating + exit rules + risk limits) implemented in the live code,
but as a pure-Python, self-contained, synchronous engine — no DB, no async,
no FastAPI dependency.

Regime blocks
-------------
* GOOD  — win bias 0.65–0.72, lasts 20–50 trades
* CHOP  — win bias 0.48–0.53, lasts 15–40 trades
* BAD   — win bias 0.32–0.42, lasts 15–35 trades
* SPIKE — ATR-spike event injected into any regime; forces expansion exit
          for the duration of the spike block (3–8 trades)

Usage
-----
    python scripts/stress_test.py                   # 500 sims × 252 days
    python scripts/stress_test.py -n 100 -d 126    # 100 sims × 126 days (6 mo)
    python scripts/stress_test.py -n 50  --seed 42 # reproducible run

Output
------
* Console: formatted table + recommended tuning analysis
* data/stress_results.json: machine-readable per-simulation + aggregate stats
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ──────────────────────────────────────────────────────────────────────────────
# Configuration mirror (no import from app — fully self-contained)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SimConfig:
    """Mirrors app/config.py — change these to match your .env."""

    # Account
    starting_balance: float = 10_000.0

    # Risk sizing (Mode C)
    defensive_risk_pct: float = 0.5
    expansion_risk_pct: float = 0.9
    tp_rr_ratio: float = 1.8

    # Consecutive-loss scaling
    consecutive_loss_threshold: int = 3
    consecutive_loss_scale_factor: float = 0.5

    # Daily loss limit
    max_daily_loss_pct: float = 2.0

    # Intraday DD hard stop
    intraday_dd_stop_pct: float = 5.0

    # Total-drawdown kill-switch
    max_total_drawdown_pct: float = 10.0

    # Expansion activation gates
    expansion_min_trades: int = 30
    expansion_min_win_rate: float = 0.60
    expansion_max_dd_pct: float = 3.0   # rolling max-DD threshold for gate

    # Expansion window
    expansion_max_trades: int = 20
    expansion_risk_pct_value: float = 0.9   # alias for use in self methods

    # Expansion exit gates
    expansion_exit_consec_losses: int = 3
    expansion_exit_dd_pct: float = 3.0

    # Simulation length
    sim_days: int = 252   # ~12 months
    trades_per_day_min: int = 2
    trades_per_day_max: int = 6


# ──────────────────────────────────────────────────────────────────────────────
# Regime definitions
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeBlock:
    regime: str            # "good" | "chop" | "bad" | "spike"
    win_prob: float
    length_trades: int     # number of trades this block lasts
    atr_spike: bool = False


def _build_regime_schedule(rng: random.Random, total_trades: int) -> list[tuple[str, float, bool]]:
    """
    Returns a flat list of (regime, win_prob, atr_spike) one entry per trade.
    """
    schedule: list[tuple[str, float, bool]] = []

    while len(schedule) < total_trades:
        regime = rng.choices(
            ["good", "chop", "bad"],
            weights=[35, 40, 25],
        )[0]

        if regime == "good":
            win_prob = rng.uniform(0.65, 0.72)
            length   = rng.randint(20, 50)
        elif regime == "chop":
            win_prob = rng.uniform(0.48, 0.53)
            length   = rng.randint(15, 40)
        else:  # bad
            win_prob = rng.uniform(0.32, 0.42)
            length   = rng.randint(15, 35)

        # Occasionally inject an ATR spike in the first part of the block
        spike_trade = rng.randint(1, length) if rng.random() < 0.08 else -1

        for i in range(length):
            if len(schedule) >= total_trades:
                break
            atr_spike = (i == spike_trade)
            schedule.append((regime, win_prob, atr_spike))

    return schedule[:total_trades]


# ──────────────────────────────────────────────────────────────────────────────
# Rolling window stats  (mirrors storage.get_rolling_trade_stats)
# ──────────────────────────────────────────────────────────────────────────────

def _compute_rolling_stats(pnl_window: deque[float]) -> dict:
    """
    Compute rolling-window stats from a deque of PnL values.
    Mirrors the SQL implementation in storage.get_rolling_trade_stats().
    """
    total = len(pnl_window)
    if total == 0:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "max_dd_pct": 0.0}

    wins   = sum(1 for p in pnl_window if p > 0)
    losses = total - wins
    win_rate = wins / total

    # Peak-to-trough rolling drawdown (from running equity on the window)
    running = 0.0
    peak    = 0.0
    max_dd  = 0.0
    for pnl in pnl_window:
        running += pnl
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    # Express drawdown as % of starting running equity (use abs starting balance proxy)
    # In real code, base equity is account balance — here we use 10_000 as proxy
    max_dd_pct = (max_dd / 10_000.0) * 100.0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "max_dd_pct": max_dd_pct,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Mode C expansion state machine  (pure-Python, no async)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ExpState:
    active: bool = False
    start_equity: float = 0.0
    trades_in_window: int = 0
    exp_consecutive_losses: int = 0   # losses *inside* expansion window
    exit_reason: str = ""
    atr_spike_active: bool = False


def _expansion_after_trade(
    state: ExpState,
    account_equity: float,
    peak_equity: float,
    rolling_stats: dict,
    is_win: bool,
    atr_spike: bool,
    cfg: SimConfig,
) -> ExpState:
    """Pure-Python port of expansion_manager.after_trade()."""
    s = ExpState(
        active=state.active,
        start_equity=state.start_equity,
        trades_in_window=state.trades_in_window,
        exp_consecutive_losses=state.exp_consecutive_losses,
        exit_reason=state.exit_reason,
        atr_spike_active=atr_spike,  # set from current trade
    )

    if s.active:
        # Update window counters
        s.trades_in_window += 1
        if is_win:
            s.exp_consecutive_losses = 0
        else:
            s.exp_consecutive_losses += 1

        # Check exit conditions
        exit_reason = ""

        if s.trades_in_window >= cfg.expansion_max_trades:
            exit_reason = f"window_exhausted ({s.trades_in_window})"
        elif s.exp_consecutive_losses >= cfg.expansion_exit_consec_losses:
            exit_reason = f"consecutive_losses ({s.exp_consecutive_losses})"
        elif s.start_equity > 0:
            dd_pct = (s.start_equity - account_equity) / s.start_equity * 100.0
            if dd_pct >= cfg.expansion_exit_dd_pct:
                exit_reason = f"drawdown_from_start ({dd_pct:.2f}%)"
        if not exit_reason and atr_spike:
            exit_reason = "atr_spike"

        if exit_reason:
            s.active = False
            s.exit_reason = exit_reason

    else:
        # Check activation conditions
        can_activate = (
            rolling_stats["total"] >= cfg.expansion_min_trades
            and rolling_stats["win_rate"] >= cfg.expansion_min_win_rate
            and rolling_stats["max_dd_pct"] <= cfg.expansion_max_dd_pct
            and account_equity >= peak_equity   # must be at a new high
            and not atr_spike
        )
        if can_activate:
            s.active = True
            s.start_equity = account_equity
            s.trades_in_window = 0
            s.exp_consecutive_losses = 0
            s.exit_reason = ""

    return s


# ──────────────────────────────────────────────────────────────────────────────
# Results containers
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DayRecord:
    day_index: int
    trades: int
    day_pnl: float
    equity_close: float
    daily_lock_hit: bool
    intraday_dd_hit: bool
    kill_switch_hit: bool
    expansion_active_pct: float   # fraction of trades today in expansion

@dataclass
class SimResult:
    seed: int
    # Final state
    final_equity: float
    total_trades: int
    total_days_simulated: int

    # Drawdown
    max_drawdown_pct: float          # peak-to-trough % drawdown over full sim
    max_drawdown_abs: float

    # Lock / kill tracking
    daily_lock_days: int             # days where daily lock triggered
    intraday_dd_days: int            # days where intraday DD stop triggered
    kill_switch_triggered: bool
    equity_at_kill: float | None     # equity when kill-switch fired, else None
    kill_switch_pct_below_peak: float | None  # % below peak at kill

    # Expansion stats
    expansion_activations: int
    total_trades_in_expansion: int
    total_trades_in_defensive: int
    expansion_exit_reasons: dict[str, int]  # reason → count

    # Avg trades / day
    avg_trades_per_day: float

    # Regime breakdown
    trades_by_regime: dict[str, int]  # regime → count

    # Daily equity trail (for distribution analysis)
    daily_equities: list[float] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Core simulation runner
# ──────────────────────────────────────────────────────────────────────────────

def run_one_simulation(seed: int, cfg: SimConfig) -> SimResult:
    rng = random.Random(seed)

    # Estimate total trades to pre-build regime schedule
    avg_trades_day = (cfg.trades_per_day_min + cfg.trades_per_day_max) / 2
    max_possible_trades = int(cfg.sim_days * cfg.trades_per_day_max * 1.1)
    regime_schedule = _build_regime_schedule(rng, max_possible_trades)
    regime_idx = 0

    # Account state
    equity     = cfg.starting_balance
    peak_equity = equity
    balance    = equity

    # Rolling window: deque of last N trade PnLs
    rolling_pnl: deque[float] = deque(maxlen=cfg.expansion_min_trades)

    # Expansion state
    exp_state = ExpState()

    # Global consecutive losses (for defensive scaling)
    global_consec_losses = 0

    # Tracking
    total_trades = 0
    total_days_sim = 0
    max_dd_pct = 0.0
    max_dd_abs = 0.0
    kill_switch_triggered = False
    equity_at_kill: float | None = None
    kill_pct_below_peak: float | None = None
    daily_lock_days = 0
    intraday_dd_days = 0
    expansion_activations = 0
    prev_exp_active = False
    total_exp_trades = 0
    total_def_trades = 0
    expansion_exit_reasons: dict[str, int] = {}
    trades_by_regime: dict[str, int] = {"good": 0, "chop": 0, "bad": 0}
    daily_equities: list[float] = []

    for day_idx in range(cfg.sim_days):
        if kill_switch_triggered:
            break

        total_days_sim += 1
        equity_at_day_open = equity
        daily_pnl = 0.0
        n_trades_today = rng.randint(cfg.trades_per_day_min, cfg.trades_per_day_max)

        daily_lock_today    = False
        intraday_dd_today   = False
        exp_trades_today    = 0

        for _ in range(n_trades_today):
            # ── Guard: kill-switch ─────────────────────────────────────────
            dd_from_peak = (peak_equity - equity) / peak_equity * 100.0
            if dd_from_peak >= cfg.max_total_drawdown_pct:
                kill_switch_triggered = True
                equity_at_kill = equity
                kill_pct_below_peak = dd_from_peak
                break

            # ── Guard: daily loss limit ────────────────────────────────────
            daily_loss_limit = -(equity * cfg.max_daily_loss_pct / 100.0)
            if daily_pnl <= daily_loss_limit:
                daily_lock_today = True
                break

            # ── Guard: intraday DD hard stop ───────────────────────────────
            intraday_loss_limit = -(equity_at_day_open * cfg.intraday_dd_stop_pct / 100.0)
            intraday_pnl = equity - equity_at_day_open
            if intraday_pnl <= intraday_loss_limit:
                intraday_dd_today = True
                break

            # ── Get regime for this trade ──────────────────────────────────
            if regime_idx >= len(regime_schedule):
                # Extend schedule if needed
                extra = _build_regime_schedule(rng, 1000)
                regime_schedule.extend(extra)

            regime_label, win_prob, atr_spike = regime_schedule[regime_idx]
            regime_idx += 1

            if regime_label in trades_by_regime:
                trades_by_regime[regime_label] += 1

            # ── Determine outcome ──────────────────────────────────────────
            is_win = rng.random() < win_prob

            # ── Compute PnL (Mode C aware) ─────────────────────────────────
            if exp_state.active:
                risk_pct = cfg.expansion_risk_pct
            else:
                if global_consec_losses >= cfg.consecutive_loss_threshold:
                    risk_pct = cfg.defensive_risk_pct * cfg.consecutive_loss_scale_factor
                else:
                    risk_pct = cfg.defensive_risk_pct

            risk_amount = equity * (risk_pct / 100.0)
            pnl = round(risk_amount * cfg.tp_rr_ratio if is_win else -risk_amount, 4)

            # Add a small random slippage/commission noise (±0.5% of risk)
            pnl += rng.uniform(-0.005, 0.005) * abs(pnl)

            # ── Update account ─────────────────────────────────────────────
            equity     += pnl
            balance    += pnl
            daily_pnl  += pnl
            equity = max(equity, 0.0)   # no negative equity

            if equity > peak_equity:
                peak_equity = equity

            # ── Update global consecutive losses ───────────────────────────
            if is_win:
                global_consec_losses = 0
            else:
                global_consec_losses += 1

            # ── Rolling window ─────────────────────────────────────────────
            rolling_pnl.append(pnl)
            total_trades += 1

            # ── Track expansion mode ───────────────────────────────────────
            if exp_state.active:
                total_exp_trades += 1
                exp_trades_today  += 1
            else:
                total_def_trades += 1

            # ── Update expansion state ─────────────────────────────────────
            rolling_stats = _compute_rolling_stats(rolling_pnl)
            new_exp = _expansion_after_trade(
                state=exp_state,
                account_equity=equity,
                peak_equity=peak_equity,
                rolling_stats=rolling_stats,
                is_win=is_win,
                atr_spike=atr_spike,
                cfg=cfg,
            )

            # Detect activation event
            if new_exp.active and not prev_exp_active:
                expansion_activations += 1

            # Detect exit event — record reason
            if not new_exp.active and prev_exp_active and new_exp.exit_reason:
                bucket = new_exp.exit_reason.split(" (")[0]   # normalise
                expansion_exit_reasons[bucket] = expansion_exit_reasons.get(bucket, 0) + 1

            prev_exp_active = new_exp.active
            exp_state = new_exp

        # ── End of day ─────────────────────────────────────────────────────

        # Track max drawdown
        current_dd_pct = (peak_equity - equity) / peak_equity * 100.0
        current_dd_abs = peak_equity - equity
        if current_dd_pct > max_dd_pct:
            max_dd_pct = current_dd_pct
        if current_dd_abs > max_dd_abs:
            max_dd_abs = current_dd_abs

        if daily_lock_today:
            daily_lock_days += 1
        if intraday_dd_today:
            intraday_dd_days += 1

        daily_equities.append(round(equity, 2))

    avg_trades_per_day = total_trades / max(total_days_sim, 1)

    return SimResult(
        seed=seed,
        final_equity=round(equity, 2),
        total_trades=total_trades,
        total_days_simulated=total_days_sim,
        max_drawdown_pct=round(max_dd_pct, 4),
        max_drawdown_abs=round(max_dd_abs, 2),
        daily_lock_days=daily_lock_days,
        intraday_dd_days=intraday_dd_days,
        kill_switch_triggered=kill_switch_triggered,
        equity_at_kill=round(equity_at_kill, 2) if equity_at_kill else None,
        kill_switch_pct_below_peak=round(kill_pct_below_peak, 4) if kill_pct_below_peak else None,
        expansion_activations=expansion_activations,
        total_trades_in_expansion=total_exp_trades,
        total_trades_in_defensive=total_def_trades,
        expansion_exit_reasons=dict(sorted(expansion_exit_reasons.items())),
        avg_trades_per_day=round(avg_trades_per_day, 2),
        trades_by_regime=trades_by_regime,
        daily_equities=daily_equities,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Aggregate statistics
# ──────────────────────────────────────────────────────────────────────────────

def _percentile(data: list[float], pct: float) -> float:
    """Compute the pct-th percentile of sorted data (0–100)."""
    if not data:
        return 0.0
    s = sorted(data)
    idx = (pct / 100.0) * (len(s) - 1)
    lo  = int(idx)
    hi  = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def run_stress_test(
    n_sims: int,
    cfg: SimConfig,
    base_seed: int = 0,
    verbose: bool = True,
) -> dict[str, Any]:
    """
    Run n_sims independent simulations and return aggregate statistics.
    Each simulation gets seed = base_seed + i (reproducible).
    """
    results: list[SimResult] = []

    for i in range(n_sims):
        r = run_one_simulation(seed=base_seed + i, cfg=cfg)
        results.append(r)
        if verbose and (i + 1) % max(1, n_sims // 10) == 0:
            pct_done = (i + 1) / n_sims * 100
            print(f"  {pct_done:5.1f}%  sim {i+1:>5}/{n_sims}  "
                  f"last_equity=${r.final_equity:>10,.2f}  "
                  f"max_dd={r.max_drawdown_pct:.2f}%  "
                  f"killed={'YES' if r.kill_switch_triggered else ' no'}",
                  flush=True)

    # ── Aggregate ──────────────────────────────────────────────────────────
    final_equities    = [r.final_equity for r in results]
    max_dds           = [r.max_drawdown_pct for r in results]
    kill_runs         = [r for r in results if r.kill_switch_triggered]
    daily_lock_rates  = [r.daily_lock_days / max(r.total_days_simulated, 1) for r in results]
    exp_activations   = [r.expansion_activations for r in results]
    avg_trades        = [r.avg_trades_per_day for r in results]

    # Days in defensive vs expansion
    exp_day_frac = []
    for r in results:
        tot = r.total_trades_in_expansion + r.total_trades_in_defensive
        exp_day_frac.append(r.total_trades_in_expansion / tot if tot > 0 else 0.0)

    # Exit reasons aggregated
    all_exit_reasons: dict[str, int] = {}
    for r in results:
        for k, v in r.expansion_exit_reasons.items():
            all_exit_reasons[k] = all_exit_reasons.get(k, 0) + v

    total_exits = sum(all_exit_reasons.values())

    summary = {
        "meta": {
            "n_sims": n_sims,
            "sim_days": cfg.sim_days,
            "starting_balance": cfg.starting_balance,
            "base_seed": base_seed,
        },
        "equity_distribution": {
            "min":    round(_percentile(final_equities,  0), 2),
            "p5":     round(_percentile(final_equities,  5), 2),
            "p10":    round(_percentile(final_equities, 10), 2),
            "p25":    round(_percentile(final_equities, 25), 2),
            "median": round(_percentile(final_equities, 50), 2),
            "p75":    round(_percentile(final_equities, 75), 2),
            "p90":    round(_percentile(final_equities, 90), 2),
            "p95":    round(_percentile(final_equities, 95), 2),
            "max":    round(_percentile(final_equities, 100), 2),
            "pct_profitable": round(
                sum(1 for e in final_equities if e > cfg.starting_balance) / n_sims * 100, 1
            ),
        },
        "drawdown_distribution": {
            "min":    round(_percentile(max_dds,  0), 3),
            "p50":    round(_percentile(max_dds, 50), 3),
            "p90":    round(_percentile(max_dds, 90), 3),
            "p95":    round(_percentile(max_dds, 95), 3),
            "p99":    round(_percentile(max_dds, 99), 3),
            "max":    round(_percentile(max_dds, 100), 3),
        },
        "locks": {
            "pct_runs_hitting_daily_lock": round(
                sum(1 for r in results if r.daily_lock_days > 0) / n_sims * 100, 1
            ),
            "pct_runs_hitting_intraday_dd": round(
                sum(1 for r in results if r.intraday_dd_days > 0) / n_sims * 100, 1
            ),
            "pct_runs_hitting_kill_switch": round(len(kill_runs) / n_sims * 100, 1),
            "avg_daily_lock_days_per_sim": round(
                sum(r.daily_lock_days for r in results) / n_sims, 2
            ),
            "avg_intraday_dd_days_per_sim": round(
                sum(r.intraday_dd_days for r in results) / n_sims, 2
            ),
        },
        "kill_switch": {
            "triggered_count": len(kill_runs),
            "avg_equity_at_kill": round(
                sum(r.equity_at_kill for r in kill_runs) / len(kill_runs), 2
            ) if kill_runs else None,
            "avg_pct_below_peak_at_kill": round(
                sum(r.kill_switch_pct_below_peak for r in kill_runs) / len(kill_runs), 4
            ) if kill_runs else None,
        },
        "expansion": {
            "avg_activations_per_sim": round(sum(exp_activations) / n_sims, 2),
            "pct_sims_with_any_activation": round(
                sum(1 for x in exp_activations if x > 0) / n_sims * 100, 1
            ),
            "avg_pct_trades_in_expansion": round(
                sum(exp_day_frac) / n_sims * 100, 1
            ),
            "avg_pct_trades_in_defensive": round(
                (1 - sum(exp_day_frac) / n_sims) * 100, 1
            ),
            "exit_reason_counts": all_exit_reasons,
            "exit_reason_pcts": {
                k: round(v / total_exits * 100, 1)
                for k, v in all_exit_reasons.items()
            } if total_exits else {},
        },
        "trades": {
            "avg_per_day": round(sum(avg_trades) / n_sims, 2),
            "p5_per_day":  round(_percentile(avg_trades,  5), 2),
            "p95_per_day": round(_percentile(avg_trades, 95), 2),
        },
        "per_sim": [
            {
                "seed": r.seed,
                "final_equity": r.final_equity,
                "max_dd_pct": r.max_drawdown_pct,
                "kill_switch": r.kill_switch_triggered,
                "expansion_activations": r.expansion_activations,
                "daily_lock_days": r.daily_lock_days,
                "intraday_dd_days": r.intraday_dd_days,
                "total_trades": r.total_trades,
            }
            for r in results
        ],
    }

    return summary


# ──────────────────────────────────────────────────────────────────────────────
# Console report
# ──────────────────────────────────────────────────────────────────────────────

def print_report(s: dict[str, Any]) -> None:
    W = 62
    sep    = "─" * W
    thick  = "═" * W

    def row(label: str, value: str) -> str:
        dots = "." * max(1, W - 4 - len(label) - len(value))
        return f"  {label} {dots} {value}"

    print()
    print(f"  {'BNK_TRADESYSTEM — STRESS TEST RESULTS':^{W}}")
    print(thick)

    m = s["meta"]
    print(row("Simulations",     f"{m['n_sims']}"))
    print(row("Sim length",      f"{m['sim_days']} trading days (~{m['sim_days']//21} months)"))
    print(row("Starting balance",f"${m['starting_balance']:>10,.2f}"))
    print(sep)

    # Equity
    ed = s["equity_distribution"]
    print(f"  {'FINAL EQUITY DISTRIBUTION':}")
    print(row("  Min",           f"${ed['min']:>10,.2f}"))
    print(row("  P5  (worst 5%)",f"${ed['p5']:>10,.2f}"))
    print(row("  P25",           f"${ed['p25']:>10,.2f}"))
    print(row("  Median",        f"${ed['median']:>10,.2f}"))
    print(row("  P75",           f"${ed['p75']:>10,.2f}"))
    print(row("  P95",           f"${ed['p95']:>10,.2f}"))
    print(row("  Max",           f"${ed['max']:>10,.2f}"))
    print(row("  % profitable",  f"{ed['pct_profitable']}%"))
    print(sep)

    # Drawdown
    dd = s["drawdown_distribution"]
    print(f"  {'MAX DRAWDOWN DISTRIBUTION':}")
    print(row("  Median",  f"{dd['p50']:.2f}%"))
    print(row("  P90",     f"{dd['p90']:.2f}%"))
    print(row("  P95",     f"{dd['p95']:.2f}%"))
    print(row("  P99",     f"{dd['p99']:.2f}%"))
    print(row("  Absolute worst", f"{dd['max']:.2f}%"))
    print(sep)

    # Locks
    lk = s["locks"]
    ks = s["kill_switch"]
    print(f"  {'PROTECTION TRIGGERS':}")
    print(row("  Runs hitting daily lock",     f"{lk['pct_runs_hitting_daily_lock']}%"))
    print(row("  Runs hitting intraday DD",    f"{lk['pct_runs_hitting_intraday_dd']}%"))
    print(row("  Runs hitting kill-switch",    f"{lk['pct_runs_hitting_kill_switch']}%"))
    print(row("  Avg daily-lock days/sim",     f"{lk['avg_daily_lock_days_per_sim']:.1f}"))
    print(row("  Avg intraday-DD days/sim",    f"{lk['avg_intraday_dd_days_per_sim']:.1f}"))
    if ks["triggered_count"]:
        print(row("  Avg equity at kill",
                  f"${ks['avg_equity_at_kill']:>10,.2f}"))
        print(row("  Avg % below peak at kill",
                  f"{ks['avg_pct_below_peak_at_kill']:.2f}%"))
    print(sep)

    # Expansion
    ex = s["expansion"]
    print(f"  {'MODE C — EXPANSION STATISTICS':}")
    print(row("  Sims with ≥1 activation", f"{ex['pct_sims_with_any_activation']}%"))
    print(row("  Avg activations / sim",   f"{ex['avg_activations_per_sim']:.1f}"))
    print(row("  Avg % trades in expansion", f"{ex['avg_pct_trades_in_expansion']:.1f}%"))
    print(row("  Avg % trades in defensive", f"{ex['avg_pct_trades_in_defensive']:.1f}%"))

    if ex["exit_reason_pcts"]:
        print(f"\n  Expansion exit reasons:")
        for reason, pct in sorted(ex["exit_reason_pcts"].items(),
                                  key=lambda x: -x[1]):
            count = ex["exit_reason_counts"][reason]
            print(f"    {reason:<35} {pct:>5.1f}%  (n={count})")
    print(sep)

    # Trades
    tr = s["trades"]
    print(f"  {'TRADE VOLUME':}")
    print(row("  Avg trades/day",  f"{tr['avg_per_day']:.2f}"))
    print(row("  P5 trades/day",   f"{tr['p5_per_day']:.2f}"))
    print(row("  P95 trades/day",  f"{tr['p95_per_day']:.2f}"))
    print(sep)

    # Tuning recommendations
    _print_tuning_recommendations(s)
    print(thick)
    print()


def _print_tuning_recommendations(s: dict[str, Any]) -> None:
    sep = "─" * 62
    print(f"\n  {'TUNING RECOMMENDATIONS  (read-only — defaults unchanged)':}")
    print(sep)
    recs: list[str] = []

    ed = s["equity_distribution"]
    dd = s["drawdown_distribution"]
    lk = s["locks"]
    ex = s["expansion"]
    ks = s["kill_switch"]

    # Kill-switch rate
    ks_pct = lk["pct_runs_hitting_kill_switch"]
    if ks_pct > 15:
        recs.append(
            f"⚠ Kill-switch fires in {ks_pct}% of runs — consider tightening "
            f"max_total_drawdown_pct (try 7–8%) or lowering expansion_risk_pct."
        )
    elif ks_pct < 1:
        recs.append(
            f"✓ Kill-switch fires in only {ks_pct}% of runs — very durable."
        )
    else:
        recs.append(
            f"✓ Kill-switch rate {ks_pct}% is within acceptable range (1–15%)."
        )

    # Max DD p95
    p95_dd = dd["p95"]
    if p95_dd > 9:
        recs.append(
            f"⚠ P95 max-drawdown={p95_dd:.1f}% — consider tightening intraday_dd_stop_pct "
            f"(currently 5%) or max_daily_loss_pct (currently 2%)."
        )
    elif p95_dd < 4:
        recs.append(
            f"✓ P95 max-drawdown={p95_dd:.1f}% — very controlled. "
            f"You have room to raise risk slightly if signal quality improves."
        )
    else:
        recs.append(f"✓ P95 max-drawdown={p95_dd:.1f}% — within acceptable 4–9% target.")

    # P5 equity
    p5_eq = ed["p5"]
    pct_change = (p5_eq - s["meta"]["starting_balance"]) / s["meta"]["starting_balance"] * 100
    if p5_eq < s["meta"]["starting_balance"] * 0.8:
        recs.append(
            f"⚠ Worst-5% equity=${p5_eq:,.0f} ({pct_change:.1f}% of start). "
            f"System survives but worst-case is painful. "
            f"Verify bad-regime parameters are realistic."
        )
    else:
        recs.append(
            f"✓ Worst-5% equity=${p5_eq:,.0f} ({pct_change:+.1f}%)  — durable in tail scenarios."
        )

    # Daily lock frequency
    dl_pct = lk["pct_runs_hitting_daily_lock"]
    if dl_pct > 80:
        recs.append(
            f"⚠ Daily lock fires in {dl_pct}% of sims — normal (daily stops expected). "
            f"Avg {lk['avg_daily_lock_days_per_sim']:.1f} days/sim locked. "
            f"If avg > 15 days consider raising max_daily_loss_pct slightly."
        )

    # Expansion usage
    exp_pct = ex["avg_pct_trades_in_expansion"]
    if exp_pct < 10:
        recs.append(
            f"⚠ Expansion only active for {exp_pct:.1f}% of trades avg — "
            f"gates may be too tight. Consider lowering expansion_min_win_rate "
            f"to 0.57 or expansion_max_dd_pct to 4.0% to earn expansion more often."
        )
    elif exp_pct > 50:
        recs.append(
            f"⚠ Expansion active {exp_pct:.1f}% of trades — "
            f"consider raising expansion_min_win_rate to 0.63 to earn it less easily."
        )
    else:
        recs.append(
            f"✓ Expansion earns {exp_pct:.1f}% of trades — balanced activation frequency."
        )

    # Exit reason balance
    exit_reasons = ex.get("exit_reason_pcts", {})
    if exit_reasons:
        top_reason = max(exit_reasons, key=lambda k: exit_reasons[k])
        top_pct = exit_reasons[top_reason]
        if top_reason == "consecutive_losses" and top_pct > 60:
            recs.append(
                f"ℹ {top_pct:.0f}% of expansions exit via consecutive_losses — "
                f"primary exit-gate working correctly. "
                f"Consider expansion_exit_consec_losses=3 if signal quality improves."
            )
        elif top_reason == "window_exhausted" and top_pct > 40:
            recs.append(
                f"✓ {top_pct:.0f}% of expansions run full window — system earns "
                f"the maximum 20-trade bonus frequently when active."
            )

    # Median return
    median_return_pct = (ed["median"] - s["meta"]["starting_balance"]) / s["meta"]["starting_balance"] * 100
    if median_return_pct > 0:
        recs.append(
            f"✓ Median return = {median_return_pct:.1f}% over {s['meta']['sim_days']//21} months "
            f"— positive expectancy confirmed."
        )
    else:
        recs.append(
            f"⚠ Median return = {median_return_pct:.1f}% — borderline. "
            f"Signal improvement recommended before live deployment."
        )

    for rec in recs:
        # Wrap long lines
        while len(rec) > 60:
            cut = rec[:60].rfind(" ")
            if cut < 10:
                cut = 60
            print(f"  {rec[:cut]}")
            rec = "    " + rec[cut:].lstrip()
        print(f"  {rec}")


# ──────────────────────────────────────────────────────────────────────────────
# Save JSON
# ──────────────────────────────────────────────────────────────────────────────

def save_json(summary: dict[str, Any], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"  Results saved → {p.resolve()}")


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="BNK_TRADESYSTEM Stress Test Harness",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-n", "--n-sims",  type=int, default=500,
                        help="Number of independent simulations to run.")
    parser.add_argument("-d", "--days",    type=int, default=252,
                        help="Trading days per simulation (252 ≈ 12 months).")
    parser.add_argument("--seed",          type=int, default=0,
                        help="Base random seed (seed+i used per simulation).")
    parser.add_argument("--balance",       type=float, default=10_000.0,
                        help="Starting account balance in USD.")
    parser.add_argument("--out",           type=str,
                        default="data/stress_results.json",
                        help="Output path for JSON results.")
    parser.add_argument("-q", "--quiet",   action="store_true",
                        help="Suppress per-simulation progress output.")

    args = parser.parse_args()

    cfg = SimConfig(
        starting_balance=args.balance,
        sim_days=args.days,
    )

    print()
    print(f"  BNK_TRADESYSTEM — Stress Test Harness")
    print(f"  Running {args.n_sims} simulations × {args.days} days ...")
    print()

    t0 = time.perf_counter()
    summary = run_stress_test(
        n_sims=args.n_sims,
        cfg=cfg,
        base_seed=args.seed,
        verbose=not args.quiet,
    )
    elapsed = time.perf_counter() - t0

    print(f"\n  Completed {args.n_sims} sims in {elapsed:.1f}s "
          f"({args.n_sims / elapsed:.0f} sims/sec)")

    print_report(summary)
    save_json(summary, args.out)


if __name__ == "__main__":
    main()
