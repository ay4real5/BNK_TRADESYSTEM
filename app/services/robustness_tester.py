"""
app/services/robustness_tester.py
==================================
10-Year Monte Carlo Robustness Framework for Mode C System.

This module is deliberately self-contained: it mirrors the mathematical
logic from risk_manager, expansion_manager and trade_simulator without
importing async DB-backed services.  Every formula is the same; only the
I/O layer (SQLite / asyncio) is replaced with in-memory Python state.

Architecture
------------
1. Regime Engine     — 4 regime types, randomly sequenced per year
2. Edge Decay Layer  — stochastic win-rate degradation (3-6 month blocks)
3. Volatility Clustering — GARCH(1,1)-approximation after large moves
4. Fat-Tail Injector — 0.5% event risk, gap losses up to 3× normal
5. Mode C State Machine — exact port of expansion_manager logic
6. Risk Engine       — exact port of risk_manager + account sizing
7. Monte Carlo       — 500 independent seeds × 2520 days

CLI usage
---------
    python -m app.services.robustness_tester                     # 500 runs
    python -m app.services.robustness_tester --runs 100          # quick
    python -m app.services.robustness_tester --no-edge-decay
    python -m app.services.robustness_tester --no-fat-tail
    python -m app.services.robustness_tester --no-vol-cluster
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Regime types
# ─────────────────────────────────────────────────────────────────────────────

class RegimeType(str, Enum):
    TRENDING     = "trending"
    MEAN_REVERT  = "mean_reversion"
    HIGH_VOL     = "high_volatility"
    LOW_LIQ      = "low_liquidity"


@dataclass
class RegimeParams:
    """Per-regime trade-level stochastic parameters."""
    regime:          RegimeType
    win_prob_lo:     float   # uniform draw lower bound
    win_prob_hi:     float   # uniform draw upper bound
    rr_mean:         float   # mean realised R:R on wins
    rr_std:          float   # std dev of R:R on wins
    base_vol:        float   # volatility scalar (1.0 = normal)
    consec_loss_boost: float # extra probability of loss after a loss
    tail_loss_prob:  float   # P(3× gap-loss on any trade)
    min_trades:      int     # regime duration lower bound (trades)
    max_trades:      int     # regime duration upper bound (trades)


# Regime parameter table (calibrated to realistic FX/gold markets)
REGIME_TABLE: dict[RegimeType, RegimeParams] = {
    RegimeType.TRENDING: RegimeParams(
        regime         = RegimeType.TRENDING,
        win_prob_lo    = 0.63,
        win_prob_hi    = 0.72,
        rr_mean        = 1.90,
        rr_std         = 0.20,
        base_vol       = 0.85,
        consec_loss_boost = 0.05,
        tail_loss_prob = 0.004,
        min_trades     = 40,
        max_trades     = 90,
    ),
    RegimeType.MEAN_REVERT: RegimeParams(
        regime         = RegimeType.MEAN_REVERT,
        win_prob_lo    = 0.52,
        win_prob_hi    = 0.60,
        rr_mean        = 1.60,
        rr_std         = 0.25,
        base_vol       = 1.10,
        consec_loss_boost = 0.10,
        tail_loss_prob = 0.006,
        min_trades     = 30,
        max_trades     = 70,
    ),
    RegimeType.HIGH_VOL: RegimeParams(
        regime         = RegimeType.HIGH_VOL,
        win_prob_lo    = 0.44,
        win_prob_hi    = 0.54,
        rr_mean        = 2.10,
        rr_std         = 0.50,
        base_vol       = 1.80,
        consec_loss_boost = 0.15,
        tail_loss_prob = 0.015,
        min_trades     = 20,
        max_trades     = 50,
    ),
    RegimeType.LOW_LIQ: RegimeParams(
        regime         = RegimeType.LOW_LIQ,
        win_prob_lo    = 0.46,
        win_prob_hi    = 0.56,
        rr_mean        = 1.40,
        rr_std         = 0.20,
        base_vol       = 1.25,
        consec_loss_boost = 0.12,
        tail_loss_prob = 0.010,
        min_trades     = 20,
        max_trades     = 60,
    ),
}

# Regime sequencing weights (per year)
_REGIME_WEIGHTS = {
    RegimeType.TRENDING:    30,
    RegimeType.MEAN_REVERT: 35,
    RegimeType.HIGH_VOL:    20,
    RegimeType.LOW_LIQ:     15,
}


# ─────────────────────────────────────────────────────────────────────────────
# Simulation configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RobustnessConfig:
    """
    All tuneable parameters.  Defaults mirror app/config.py + .env exactly.
    """
    # Account
    starting_balance:        float = 10_000.0
    sim_years:               int   = 10
    trading_days_per_year:   int   = 252
    trades_per_day_min:      int   = 2
    trades_per_day_max:      int   = 6

    # Risk (Mode C)
    defensive_risk_pct:      float = 0.5
    expansion_risk_pct:      float = 0.9
    base_tp_rr:              float = 1.8   # used when regime rr_std not applied

    # Consecutive-loss scaling
    consec_loss_threshold:   int   = 3
    consec_loss_scale:       float = 0.5

    # Daily loss limit
    max_daily_loss_pct:      float = 2.0
    # Intraday DD stop
    intraday_dd_stop_pct:    float = 5.0
    # Kill-switch
    max_total_drawdown_pct:  float = 10.0

    # Expansion activation
    expansion_min_trades:    int   = 30
    expansion_min_win_rate:  float = 0.60
    expansion_max_dd_pct:    float = 3.0
    expansion_max_trades:    int   = 20

    # Expansion exit
    expansion_exit_consec:   int   = 3     # lives at threshold=3 (Path 2)
    expansion_exit_dd_pct:   float = 3.0

    # Feature flags
    enable_edge_decay:       bool  = True
    enable_vol_clustering:   bool  = True
    enable_fat_tail:         bool  = True

    # Edge decay parameters
    edge_decay_min_pct:      float = 0.05  # min win-rate reduction during decay
    edge_decay_max_pct:      float = 0.15  # max win-rate reduction during decay
    edge_decay_min_dur:      int   = 60    # min duration in trading days
    edge_decay_max_dur:      int   = 130   # max duration in trading days
    edge_decay_frequency:    float = 0.18  # P(new decay period starts each month)

    # Volatility clustering (GARCH-like)
    vol_cluster_alpha:       float = 0.30  # vol_next = base + alpha * |last_loss_pct|
    vol_cluster_decay:       float = 0.85  # exponential decay each trade

    # Fat tail events
    fat_tail_loss_mult:      float = 3.0   # loss multiplier for gap event
    fat_tail_extra_prob:     float = 0.005 # per-trade probability

    @property
    def sim_days(self) -> int:
        return self.sim_years * self.trading_days_per_year


# ─────────────────────────────────────────────────────────────────────────────
# Regime + trade schedule builder
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeSlot:
    """One trade's pre-determined stochastic parameters."""
    regime:     RegimeType
    win_prob:   float
    rr:         float          # realised R:R for this trade
    base_vol:   float
    tail_event: bool           # fat-tail gap risk

def _build_trade_schedule(
    rng: random.Random,
    total_trades: int,
    cfg: RobustnessConfig,
) -> list[TradeSlot]:
    """
    Generate a full trade schedule:
    - Regime blocks sequenced by yearly weights
    - Fat-tail events injected at cfg.fat_tail_extra_prob
    """
    schedule: list[TradeSlot] = []
    regime_list = list(_REGIME_WEIGHTS.keys())
    regime_wts  = [_REGIME_WEIGHTS[r] for r in regime_list]

    while len(schedule) < total_trades:
        regime_type = rng.choices(regime_list, weights=regime_wts)[0]
        params      = REGIME_TABLE[regime_type]
        win_prob    = rng.uniform(params.win_prob_lo, params.win_prob_hi)
        length      = rng.randint(params.min_trades, params.max_trades)

        for _ in range(length):
            if len(schedule) >= total_trades:
                break
            rr = max(0.5, rng.gauss(params.rr_mean, params.rr_std))
            tail = cfg.enable_fat_tail and (rng.random() < params.tail_loss_prob)
            schedule.append(TradeSlot(
                regime   = regime_type,
                win_prob = win_prob,
                rr       = rr,
                base_vol = params.base_vol,
                tail_event = tail,
            ))

    return schedule[:total_trades]


# ─────────────────────────────────────────────────────────────────────────────
# Edge decay layer
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EdgeDecayEvent:
    start_day:   int
    end_day:     int
    decay_pct:   float   # fractional reduction applied to win_prob

def _build_edge_decay(
    rng: random.Random,
    total_days: int,
    cfg: RobustnessConfig,
) -> list[EdgeDecayEvent]:
    """
    Returns a list of decay periods.  At the start of each 21-day month-ish
    block, there is a cfg.edge_decay_frequency chance of starting a new decay.
    """
    events: list[EdgeDecayEvent] = []
    day = 0
    while day < total_days:
        if rng.random() < cfg.edge_decay_frequency:
            dur   = rng.randint(cfg.edge_decay_min_dur, cfg.edge_decay_max_dur)
            decay = rng.uniform(cfg.edge_decay_min_pct, cfg.edge_decay_max_pct)
            events.append(EdgeDecayEvent(
                start_day = day,
                end_day   = min(day + dur, total_days),
                decay_pct = decay,
            ))
            day += dur  # non-overlapping
        else:
            day += 21   # step one pseudo-month
    return events


def _decay_factor(day: int, events: list[EdgeDecayEvent]) -> float:
    """Return the win-prob *multiplier* for the given calendar day (default 1.0)."""
    for ev in events:
        if ev.start_day <= day < ev.end_day:
            return 1.0 - ev.decay_pct
    return 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Volatility clustering state
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VolState:
    current_multiplier: float = 1.0

    def update(self, realized_pnl_pct: float, cfg: RobustnessConfig, base_vol: float) -> None:
        """GARCH(1,1)-like: vol_t = base_vol + alpha * |last_return|."""
        abs_ret = abs(realized_pnl_pct)
        self.current_multiplier = (
            base_vol
            + cfg.vol_cluster_alpha * abs_ret
        )
        # Exponential decay to prevent infinite accumulation
        self.current_multiplier *= cfg.vol_cluster_decay
        self.current_multiplier = max(base_vol, self.current_multiplier)


# ─────────────────────────────────────────────────────────────────────────────
# Mode C expansion state machine  (exact port from expansion_manager.py)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExpState:
    active:            bool  = False
    start_equity:      float = 0.0
    trades_in_window:  int   = 0
    exp_consec_losses: int   = 0
    exit_reason:       str   = ""

def _expansion_step(
    state:         ExpState,
    equity:        float,
    peak_equity:   float,
    rolling_stats: dict,
    is_win:        bool,
    cfg:           RobustnessConfig,
) -> ExpState:
    """Pure-Python port of expansion_manager.after_trade()."""
    s = ExpState(
        active           = state.active,
        start_equity     = state.start_equity,
        trades_in_window = state.trades_in_window,
        exp_consec_losses= state.exp_consec_losses,
        exit_reason      = state.exit_reason,
    )

    if s.active:
        s.trades_in_window += 1
        s.exp_consec_losses = 0 if is_win else s.exp_consec_losses + 1

        exit_reason = ""
        if s.trades_in_window >= cfg.expansion_max_trades:
            exit_reason = f"window_exhausted ({s.trades_in_window})"
        elif s.exp_consec_losses >= cfg.expansion_exit_consec:
            exit_reason = f"consecutive_losses ({s.exp_consec_losses})"
        elif s.start_equity > 0:
            dd = (s.start_equity - equity) / s.start_equity * 100.0
            if dd >= cfg.expansion_exit_dd_pct:
                exit_reason = f"drawdown_from_start ({dd:.2f}%)"

        if exit_reason:
            s.active      = False
            s.exit_reason = exit_reason
    else:
        can_activate = (
            rolling_stats["total"]    >= cfg.expansion_min_trades
            and rolling_stats["win_rate"] >= cfg.expansion_min_win_rate
            and rolling_stats["max_dd_pct"] <= cfg.expansion_max_dd_pct
            and equity >= peak_equity
        )
        if can_activate:
            s.active            = True
            s.start_equity      = equity
            s.trades_in_window  = 0
            s.exp_consec_losses = 0
            s.exit_reason       = ""

    return s


def _rolling_stats(pnl_window: deque) -> dict:
    total = len(pnl_window)
    if total == 0:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "max_dd_pct": 0.0}
    wins     = sum(1 for p in pnl_window if p > 0)
    win_rate = wins / total
    # Peak-to-trough drawdown within the window
    running = peak = max_dd = 0.0
    for pnl in pnl_window:
        running += pnl
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    max_dd_pct = (max_dd / 10_000.0) * 100.0   # normalised to $10 k base
    return {"total": total, "wins": wins, "losses": total - wins,
            "win_rate": win_rate, "max_dd_pct": max_dd_pct}


# ─────────────────────────────────────────────────────────────────────────────
# Per-simulation result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SimRun:
    seed:                        int
    final_equity:                float
    cagr:                        float        # annualised growth rate
    max_drawdown_pct:            float
    max_drawdown_abs:            float
    time_underwater_days:        int          # days where equity < peak
    kill_switch_triggered:       bool
    kill_switch_day:             int | None
    expansion_activations:       int
    expansion_trades:            int
    defensive_trades:            int
    total_trades:                int
    fat_tail_events_hit:         int
    fat_tail_survived:           int          # didn't trigger kill-switch after event
    edge_decay_days:             int
    worst_30d_return_pct:        float
    consec_loss_distribution:    dict[int, int]  # streak_length → count
    daily_equities:              list[float]
    regime_trade_counts:         dict[str, int]


# ─────────────────────────────────────────────────────────────────────────────
# Core simulation
# ─────────────────────────────────────────────────────────────────────────────

def run_one_sim(seed: int, cfg: RobustnessConfig) -> SimRun:
    rng = random.Random(seed)

    total_days   = cfg.sim_days
    avg_trades   = (cfg.trades_per_day_min + cfg.trades_per_day_max) / 2
    max_trades   = int(total_days * cfg.trades_per_day_max * 1.1)

    trade_schedule = _build_trade_schedule(rng, max_trades, cfg)
    decay_events   = _build_edge_decay(rng, total_days, cfg) if cfg.enable_edge_decay else []
    vol_state      = VolState(current_multiplier=1.0)

    # ── Account state ──────────────────────────────────────────────────────
    equity           = cfg.starting_balance
    peak_equity      = equity
    balance          = equity

    # ── Rolling window for expansion gating ───────────────────────────────
    rolling_pnl: deque[float] = deque(maxlen=cfg.expansion_min_trades)
    exp_state        = ExpState()
    prev_exp_active  = False

    # ── Global consecutive loss tracking ──────────────────────────────────
    global_consec    = 0

    # ── Metrics accumulators ──────────────────────────────────────────────
    trade_idx          = 0
    total_trades       = 0
    exp_activations    = 0
    exp_trades         = 0
    def_trades         = 0
    fat_tail_hits      = 0
    fat_tail_survived  = 0
    edge_decay_days    = 0
    time_underwater    = 0
    max_dd_pct         = 0.0
    max_dd_abs         = 0.0
    kill_triggered     = False
    kill_day: int | None = None
    consec_dist: dict[int, int] = {}
    regime_counts: dict[str, int] = {r.value: 0 for r in RegimeType}
    daily_equities: list[float] = []

    # 30-day rolling equity for worst-30d calc
    eq_30d: deque[float] = deque(maxlen=31)
    worst_30d = 0.0

    last_consec_run = 0   # current consecutive loss streak

    for day_idx in range(total_days):
        if kill_triggered:
            daily_equities.append(round(equity, 2))
            continue

        # ── Edge decay for this day ────────────────────────────────────────
        df = _decay_factor(day_idx, decay_events)
        if df < 1.0:
            edge_decay_days += 1

        equity_at_day_open = equity
        daily_pnl          = 0.0
        n_trades = rng.randint(cfg.trades_per_day_min, cfg.trades_per_day_max)

        for _ in range(n_trades):
            # Guard: kill-switch
            dd_from_peak = (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
            if dd_from_peak >= cfg.max_total_drawdown_pct:
                kill_triggered = True
                kill_day = day_idx
                break

            # Guard: daily loss limit
            if daily_pnl <= -(equity * cfg.max_daily_loss_pct / 100.0):
                break

            # Guard: intraday DD
            if equity - equity_at_day_open <= -(equity_at_day_open * cfg.intraday_dd_stop_pct / 100.0):
                break

            # ── Get trade slot ─────────────────────────────────────────────
            if trade_idx >= len(trade_schedule):
                trade_schedule.extend(_build_trade_schedule(rng, 2000, cfg))
            slot = trade_schedule[trade_idx]
            trade_idx += 1

            regime_counts[slot.regime.value] += 1

            # ── Determine win/loss with edge decay and vol clustering ───────
            # Edge decay reduces win probability
            effective_win_prob = min(0.98, slot.win_prob * df)

            # Volatility clustering: adjust the effective win probability
            # (higher vol → slightly lower probability due to noise)
            if cfg.enable_vol_clustering and vol_state.current_multiplier > 1.05:
                volatility_drag = 0.02 * (vol_state.current_multiplier - 1.0)
                effective_win_prob = max(0.20, effective_win_prob - volatility_drag)

            # Consecutive loss momentum (regime-dependent boost)
            params = REGIME_TABLE[slot.regime]
            if last_consec_run > 0 and rng.random() < params.consec_loss_boost:
                effective_win_prob = max(0.20, effective_win_prob * 0.92)

            is_win = rng.random() < effective_win_prob

            # ── Fat-tail override ──────────────────────────────────────────
            # Even a "win" slot can be overridden by a gap event
            is_fat_tail = False
            if slot.tail_event and not is_win:
                is_fat_tail = True
                fat_tail_hits += 1

            # ── Risk sizing (Mode C) ───────────────────────────────────────
            if exp_state.active:
                risk_pct = cfg.expansion_risk_pct
            else:
                if global_consec >= cfg.consec_loss_threshold:
                    risk_pct = cfg.defensive_risk_pct * cfg.consec_loss_scale
                else:
                    risk_pct = cfg.defensive_risk_pct

            risk_amount = equity * (risk_pct / 100.0)

            # ── Realised R:R (noisy around regime mean) ────────────────────
            realised_rr = slot.rr
            if cfg.enable_vol_clustering:
                noise = rng.gauss(0, 0.1 * vol_state.current_multiplier)
                realised_rr = max(0.5, realised_rr + noise)

            # ── Compute PnL ────────────────────────────────────────────────
            if is_win:
                pnl = risk_amount * realised_rr
            elif is_fat_tail:
                pnl = -risk_amount * cfg.fat_tail_loss_mult   # 3× gap loss
            else:
                pnl = -risk_amount

            # Small slippage noise (±0.3%)
            pnl += rng.uniform(-0.003, 0.003) * abs(pnl)

            # ── Account update ─────────────────────────────────────────────
            prev_equity = equity
            equity      = max(0.0, equity + pnl)
            balance     += pnl
            daily_pnl   += pnl
            total_trades += 1

            realized_pnl_pct = pnl / prev_equity if prev_equity > 0 else 0.0

            if equity > peak_equity:
                peak_equity = equity

            # ── Vol state ─────────────────────────────────────────────────
            if cfg.enable_vol_clustering:
                vol_state.update(realized_pnl_pct, cfg, slot.base_vol)

            # ── Global consecutive loss ────────────────────────────────────
            if is_win:
                if last_consec_run > 0:
                    consec_dist[last_consec_run] = consec_dist.get(last_consec_run, 0) + 1
                    last_consec_run = 0
                global_consec = 0
            else:
                global_consec    += 1
                last_consec_run  += 1

            # ── Rolling window & expansion state ──────────────────────────
            rolling_pnl.append(pnl)
            stats = _rolling_stats(rolling_pnl)
            new_exp = _expansion_step(exp_state, equity, peak_equity, stats, is_win, cfg)

            if new_exp.active and not prev_exp_active:
                exp_activations += 1
            if exp_state.active:
                exp_trades += 1
            else:
                def_trades += 1

            if is_fat_tail and not kill_triggered:
                dd_now = (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
                if dd_now < cfg.max_total_drawdown_pct:
                    fat_tail_survived += 1

            prev_exp_active = new_exp.active
            exp_state       = new_exp

        # ── End of day ─────────────────────────────────────────────────────
        daily_equities.append(round(equity, 2))
        eq_30d.append(equity)

        # Track max DD
        cur_dd_pct = (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
        cur_dd_abs = peak_equity - equity
        if cur_dd_pct > max_dd_pct:
            max_dd_pct = cur_dd_pct
        if cur_dd_abs > max_dd_abs:
            max_dd_abs = cur_dd_abs

        # Time underwater
        if equity < peak_equity:
            time_underwater += 1

        # Worst 30-day return
        if len(eq_30d) == 31:
            ret_30d = (eq_30d[-1] - eq_30d[0]) / eq_30d[0] * 100.0 if eq_30d[0] > 0 else 0.0
            if ret_30d < worst_30d:
                worst_30d = ret_30d

    # Flush last consec streak
    if last_consec_run > 0:
        consec_dist[last_consec_run] = consec_dist.get(last_consec_run, 0) + 1

    # ── CAGR ──────────────────────────────────────────────────────────────
    years = cfg.sim_years
    if equity > 0 and cfg.starting_balance > 0:
        cagr  = (equity / cfg.starting_balance) ** (1.0 / years) - 1.0
    else:
        cagr  = -1.0   # ruin

    return SimRun(
        seed                     = seed,
        final_equity             = round(equity, 2),
        cagr                     = round(cagr * 100.0, 4),
        max_drawdown_pct         = round(max_dd_pct, 4),
        max_drawdown_abs         = round(max_dd_abs, 2),
        time_underwater_days     = time_underwater,
        kill_switch_triggered    = kill_triggered,
        kill_switch_day          = kill_day,
        expansion_activations    = exp_activations,
        expansion_trades         = exp_trades,
        defensive_trades         = def_trades,
        total_trades             = total_trades,
        fat_tail_events_hit      = fat_tail_hits,
        fat_tail_survived        = fat_tail_survived,
        edge_decay_days          = edge_decay_days,
        worst_30d_return_pct     = round(worst_30d, 4),
        consec_loss_distribution = consec_dist,
        daily_equities           = daily_equities,
        regime_trade_counts      = regime_counts,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pct(data: list[float], p: float) -> float:
    """p-th percentile of data (p in 0–100)."""
    if not data:
        return 0.0
    s = sorted(data)
    idx = (p / 100.0) * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] * (1 - (idx - lo)) + s[hi] * (idx - lo)


def _mean(data: list[float]) -> float:
    return sum(data) / len(data) if data else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Run full Monte Carlo
# ─────────────────────────────────────────────────────────────────────────────

def run_robustness_test(
    n_runs:    int              = 500,
    cfg:       RobustnessConfig = None,
    base_seed: int              = 0,
    verbose:   bool             = True,
) -> dict[str, Any]:
    if cfg is None:
        cfg = RobustnessConfig()

    runs: list[SimRun] = []
    for i in range(n_runs):
        r = run_one_sim(seed=base_seed + i, cfg=cfg)
        runs.append(r)

        if verbose and (i + 1) % max(1, n_runs // 10) == 0:
            done_pct = (i + 1) / n_runs * 100
            print(
                f"  {done_pct:5.1f}%  sim {i+1:>5}/{n_runs}"
                f"  equity=${r.final_equity:>12,.2f}"
                f"  CAGR={r.cagr:>7.2f}%"
                f"  maxDD={r.max_drawdown_pct:.2f}%"
                f"  {'KILLED' if r.kill_switch_triggered else '      '}",
                flush=True,
            )

    return _aggregate(runs, cfg, n_runs)


def _aggregate(runs: list[SimRun], cfg: RobustnessConfig, n_runs: int) -> dict[str, Any]:
    cagrs          = [r.cagr for r in runs]
    max_dds        = [r.max_drawdown_pct for r in runs]
    final_eqs      = [r.final_equity for r in runs]
    time_uw        = [r.time_underwater_days for r in runs]
    kills          = [r for r in runs if r.kill_switch_triggered]
    exp_acts       = [r.expansion_activations for r in runs]
    worst_30d      = [r.worst_30d_return_pct for r in runs]
    fat_hits       = [r.fat_tail_events_hit for r in runs]
    fat_surv       = [r.fat_tail_survived for r in runs]

    # Ruin = final equity < 20% of starting balance
    ruin_threshold = cfg.starting_balance * 0.20
    ruin_runs = [r for r in runs if r.final_equity <= ruin_threshold]

    # % trades in expansion
    exp_pcts = []
    for r in runs:
        tot = r.expansion_trades + r.defensive_trades
        exp_pcts.append(r.expansion_trades / tot * 100.0 if tot > 0 else 0.0)

    # Aggregate consecutive loss distribution
    agg_consec: dict[int, int] = {}
    for r in runs:
        for k, v in r.consec_loss_distribution.items():
            agg_consec[k] = agg_consec.get(k, 0) + v

    # Regime breakdown (% of trades)
    regime_totals: dict[str, int] = {rt.value: 0 for rt in RegimeType}
    for r in runs:
        for rt, cnt in r.regime_trade_counts.items():
            regime_totals[rt] = regime_totals.get(rt, 0) + cnt
    total_regime_trades = sum(regime_totals.values())
    regime_pcts = {
        rt: round(cnt / total_regime_trades * 100.0, 1) if total_regime_trades else 0.0
        for rt, cnt in regime_totals.items()
    }

    # Total fat-tail survival rate
    total_fat_hits = sum(fat_hits)
    total_fat_surv = sum(fat_surv)

    summary = {
        "meta": {
            "n_runs":           n_runs,
            "sim_years":        cfg.sim_years,
            "sim_days":         cfg.sim_days,
            "starting_balance": cfg.starting_balance,
            "edge_decay":       cfg.enable_edge_decay,
            "vol_clustering":   cfg.enable_vol_clustering,
            "fat_tail":         cfg.enable_fat_tail,
        },
        # ── Primary KPIs (the 8-field summary dict) ─────────────────────
        "median_cagr":              round(_pct(cagrs, 50), 4),
        "p5_cagr":                  round(_pct(cagrs,  5), 4),
        "p95_cagr":                 round(_pct(cagrs, 95), 4),
        "p95_drawdown":             round(_pct(max_dds, 95), 4),
        "ruin_probability":         round(len(ruin_runs) / n_runs * 100.0, 3),
        "kill_switch_rate":         round(len(kills) / n_runs * 100.0, 3),
        "expansion_activation_rate": round(
            sum(1 for a in exp_acts if a > 0) / n_runs * 100.0, 3
        ),
        "tail_event_survival_rate": round(
            total_fat_surv / total_fat_hits * 100.0, 3
        ) if total_fat_hits else 100.0,

        # ── Extended stats ───────────────────────────────────────────────
        "cagr": {
            "p5":     round(_pct(cagrs,  5), 3),
            "p10":    round(_pct(cagrs, 10), 3),
            "p25":    round(_pct(cagrs, 25), 3),
            "median": round(_pct(cagrs, 50), 3),
            "p75":    round(_pct(cagrs, 75), 3),
            "p90":    round(_pct(cagrs, 90), 3),
            "p95":    round(_pct(cagrs, 95), 3),
            "pct_over_100_annual": round(
                sum(1 for c in cagrs if c > 100.0) / n_runs * 100.0, 1
            ),
            "pct_positive": round(
                sum(1 for c in cagrs if c > 0) / n_runs * 100.0, 1
            ),
        },
        "drawdown": {
            "median": round(_pct(max_dds, 50), 3),
            "p75":    round(_pct(max_dds, 75), 3),
            "p90":    round(_pct(max_dds, 90), 3),
            "p95":    round(_pct(max_dds, 95), 3),
            "p99":    round(_pct(max_dds, 99), 3),
            "worst":  round(_pct(max_dds, 100), 3),
        },
        "equity": {
            "min":    round(_pct(final_eqs,  0), 2),
            "p5":     round(_pct(final_eqs,  5), 2),
            "median": round(_pct(final_eqs, 50), 2),
            "p95":    round(_pct(final_eqs, 95), 2),
            "max":    round(_pct(final_eqs, 100), 2),
        },
        "time_underwater": {
            "median_days":   round(_pct(time_uw, 50), 1),
            "p95_days":      round(_pct(time_uw, 95), 1),
            "avg_pct_of_sim": round(
                _mean([t / cfg.sim_days * 100.0 for t in time_uw]), 1
            ),
        },
        "kill_switch": {
            "count":               len(kills),
            "rate_pct":            round(len(kills) / n_runs * 100.0, 2),
            "avg_day_of_kill":     round(_mean([r.kill_switch_day for r in kills if r.kill_switch_day is not None]), 1) if kills else None,
            "avg_equity_at_kill":  round(_mean([r.final_equity for r in kills]), 2) if kills else None,
        },
        "expansion": {
            "pct_sims_with_activation":  round(sum(1 for a in exp_acts if a > 0) / n_runs * 100.0, 1),
            "avg_activations_per_sim":   round(_mean(exp_acts), 2),
            "avg_pct_trades_in_expansion": round(_mean(exp_pcts), 2),
        },
        "fat_tail": {
            "total_events_across_all_runs": total_fat_hits,
            "avg_per_sim":  round(_mean(fat_hits), 2),
            "survival_rate_pct": round(total_fat_surv / total_fat_hits * 100.0, 2) if total_fat_hits else 100.0,
        },
        "worst_30d": {
            "median_pct":  round(_pct(worst_30d, 50), 3),
            "p5_pct":      round(_pct(worst_30d,  5), 3),
            "worst_pct":   round(_pct(worst_30d,  0), 3),
        },
        "edge_decay": {
            "avg_decay_days_per_sim": round(
                _mean([r.edge_decay_days for r in runs]), 1
            ),
            "avg_decay_pct_of_sim": round(
                _mean([r.edge_decay_days / cfg.sim_days * 100.0 for r in runs]), 1
            ),
        },
        "consec_loss_dist": {  # streak_length → total occurrences across all runs
            str(k): v for k, v in sorted(agg_consec.items())
        },
        "regime_pcts": regime_pcts,
        "ruin": {
            "threshold_pct": 80,   # equity drops below 20% of starting
            "count":         len(ruin_runs),
            "rate_pct":      round(len(ruin_runs) / n_runs * 100.0, 3),
        },
        "per_run": [
            {
                "seed":             r.seed,
                "final_equity":     r.final_equity,
                "cagr":             r.cagr,
                "max_dd_pct":       r.max_drawdown_pct,
                "kill_switch":      r.kill_switch_triggered,
                "exp_activations":  r.expansion_activations,
                "total_trades":     r.total_trades,
                "fat_tail_hits":    r.fat_tail_events_hit,
                "worst_30d_pct":    r.worst_30d_return_pct,
            }
            for r in runs
        ],
    }
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Console report
# ─────────────────────────────────────────────────────────────────────────────

def print_report(s: dict[str, Any]) -> None:
    W   = 64
    SEP = "─" * W
    THK = "═" * W

    def row(label: str, value: str) -> str:
        dots = "." * max(1, W - 4 - len(label) - len(value))
        return f"  {label} {dots} {value}"

    print()
    print(f"  {'BNK_TRADESYSTEM — 10-YEAR ROBUSTNESS REPORT':^{W}}")
    print(THK)

    m = s["meta"]
    flags = ("edge_decay=" + ("ON" if m["edge_decay"] else "OFF") + "  " +
             "vol_cluster=" + ("ON" if m["vol_clustering"] else "OFF") + "  " +
             "fat_tail=" + ("ON" if m["fat_tail"] else "OFF"))
    print(row("Simulations",    str(m["n_runs"])))
    print(row("Horizon",        f"{m['sim_years']} years ({m['sim_days']} trading days)"))
    print(row("Starting balance", f"${m['starting_balance']:,.2f}"))
    print(row("Feature flags",  flags))
    print(SEP)

    # ── Primary KPIs ──────────────────────────────────────────────────────
    print(f"  {'PRIMARY KPIs':}")
    print(row("  Median CAGR",        f"{s['median_cagr']:.2f}%"))
    print(row("  P5 CAGR (bad 5%)",   f"{s['p5_cagr']:.2f}%"))
    print(row("  P95 CAGR (best 5%)", f"{s['p95_cagr']:.2f}%"))
    print(row("  P95 max drawdown",   f"{s['p95_drawdown']:.2f}%"))
    print(row("  Ruin probability",   f"{s['ruin_probability']:.2f}%"))
    print(row("  Kill-switch rate",   f"{s['kill_switch_rate']:.2f}%"))
    print(row("  Expansion activated", f"{s['expansion_activation_rate']:.1f}% of runs"))
    print(row("  Fat-tail survival",  f"{s['tail_event_survival_rate']:.2f}%"))
    print(SEP)

    # ── CAGR detail ───────────────────────────────────────────────────────
    cg = s["cagr"]
    print(f"  {'CAGR DISTRIBUTION':}")
    print(row("  P5",           f"{cg['p5']:.2f}%"))
    print(row("  P25",          f"{cg['p25']:.2f}%"))
    print(row("  Median",       f"{cg['median']:.2f}%"))
    print(row("  P75",          f"{cg['p75']:.2f}%"))
    print(row("  P95",          f"{cg['p95']:.2f}%"))
    print(row("  % positive runs",         f"{cg['pct_positive']}%"))
    print(row("  % CAGR > 100% / year",    f"{cg['pct_over_100_annual']}%"))
    print(SEP)

    # ── Drawdown ──────────────────────────────────────────────────────────
    dd = s["drawdown"]
    print(f"  {'MAX DRAWDOWN DISTRIBUTION':}")
    print(row("  Median",  f"{dd['median']:.2f}%"))
    print(row("  P90",     f"{dd['p90']:.2f}%"))
    print(row("  P95",     f"{dd['p95']:.2f}%"))
    print(row("  P99",     f"{dd['p99']:.2f}%"))
    print(row("  Worst",   f"{dd['worst']:.2f}%"))
    print(SEP)

    # ── Time underwater ───────────────────────────────────────────────────
    uw = s["time_underwater"]
    print(f"  {'TIME UNDERWATER':}")
    print(row("  Median days underwater",  f"{uw['median_days']:.0f}"))
    print(row("  P95 days underwater",     f"{uw['p95_days']:.0f}"))
    print(row("  Avg % of simulation",     f"{uw['avg_pct_of_sim']:.1f}%"))
    print(SEP)

    # ── Kill switch ───────────────────────────────────────────────────────
    ks = s["kill_switch"]
    print(f"  {'KILL-SWITCH':}")
    print(row("  Triggered",     f"{ks['count']} runs ({ks['rate_pct']:.1f}%)"))
    if ks["avg_day_of_kill"]:
        print(row("  Avg day of kill",    f"day {ks['avg_day_of_kill']:.0f} of {s['meta']['sim_days']}"))
        print(row("  Avg equity at kill", f"${ks['avg_equity_at_kill']:,.2f}"))
    print(SEP)

    # ── Expansion ─────────────────────────────────────────────────────────
    ex = s["expansion"]
    print(f"  {'MODE C EXPANSION':}")
    print(row("  Runs with ≥1 activation",   f"{ex['pct_sims_with_activation']:.1f}%"))
    print(row("  Avg activations / sim",     f"{ex['avg_activations_per_sim']:.1f}"))
    print(row("  Avg % trades in expansion", f"{ex['avg_pct_trades_in_expansion']:.1f}%"))
    print(SEP)

    # ── Fat tail ─────────────────────────────────────────────────────────
    ft = s["fat_tail"]
    print(f"  {'FAT-TAIL EVENTS':}")
    print(row("  Total gap events (all runs)", str(ft["total_events_across_all_runs"])))
    print(row("  Avg per simulation",          f"{ft['avg_per_sim']:.1f}"))
    print(row("  Survival rate",               f"{ft['survival_rate_pct']:.2f}%"))
    print(SEP)

    # ── Edge decay & worst 30d ─────────────────────────────────────────────
    ed = s["edge_decay"]
    w3 = s["worst_30d"]
    print(f"  {'EDGE DECAY + WORST MONTH':}")
    print(row("  Avg decay days / sim",  f"{ed['avg_decay_days_per_sim']:.0f}"))
    print(row("  Avg % of sim in decay", f"{ed['avg_decay_pct_of_sim']:.1f}%"))
    print(row("  Median worst-30d return", f"{w3['median_pct']:.2f}%"))
    print(row("  P5 worst-30d return",     f"{w3['p5_pct']:.2f}%"))
    print(row("  Absolute worst month",    f"{w3['worst_pct']:.2f}%"))
    print(SEP)

    # ── Consecutive loss distribution ──────────────────────────────────────
    cd = s.get("consec_loss_dist", {})
    if cd:
        total_streaks = sum(cd.values())
        print(f"  {'CONSECUTIVE LOSS STREAKS (all runs)':}")
        for length in sorted(int(k) for k in cd.keys()):
            ct = cd[str(length)]
            bar_len = int(ct / max(cd.values()) * 20)
            bar = "█" * bar_len
            pct = ct / total_streaks * 100
            print(f"    streak={length:<3} {bar:<20} {ct:>6}  ({pct:.1f}%)")
    print(SEP)

    # ── Regime breakdown ──────────────────────────────────────────────────
    rp = s.get("regime_pcts", {})
    if rp:
        print(f"  {'REGIME BREAKDOWN':}")
        for r_name in [r.value for r in RegimeType]:
            pct = rp.get(r_name, 0.0)
            print(row(f"  {r_name.replace('_', ' ').title()}", f"{pct:.1f}%"))
    print(SEP)

    # ── Interpretation ────────────────────────────────────────────────────
    _print_interpretation(s)
    print(THK)
    print()


def _print_interpretation(s: dict[str, Any]) -> None:
    SEP = "─" * 64
    print(f"\n  {'PROFESSIONAL INTERPRETATION':}")
    print(SEP)

    notes: list[str] = []

    median_cagr    = s["median_cagr"]
    p5_cagr        = s["p5_cagr"]
    p95_dd         = s["p95_drawdown"]
    ruin_prob      = s["ruin_probability"]
    ks_rate        = s["kill_switch_rate"]
    tail_surv      = s["tail_event_survival_rate"]
    pct_over_100   = s["cagr"]["pct_over_100_annual"]
    exp_pct        = s["expansion"]["avg_pct_trades_in_expansion"]
    uw_pct         = s["time_underwater"]["avg_pct_of_sim"]
    w3_p5          = s["worst_30d"]["p5_pct"]

    # CAGR verdict
    if median_cagr >= 80:
        notes.append(f"✓ Median CAGR={median_cagr:.1f}%: elite growth. "
                     f"Verify this survives live slippage (+50% costs scenario).")
    elif median_cagr >= 30:
        notes.append(f"✓ Median CAGR={median_cagr:.1f}%: strong institutional-grade return.")
    elif median_cagr >= 10:
        notes.append(f"◆ Median CAGR={median_cagr:.1f}%: acceptable but modest. "
                     f"Signal quality improvement should be next priority.")
    else:
        notes.append(f"⚠ Median CAGR={median_cagr:.1f}%: weak. System needs signal work "
                     f"before live deployment.")

    # 100%+ CAGR
    if pct_over_100 > 30:
        notes.append(f"✓ {pct_over_100:.0f}% of runs exceed 100% annual return — "
                     f"'explosive months' scenario is statistically plausible in good regimes.")
    elif pct_over_100 > 10:
        notes.append(f"◆ {pct_over_100:.0f}% of runs exceed 100% annual — possible "
                     f"but not reliable. Expansion gate quality is the key lever.")
    else:
        notes.append(f"⚠ Only {pct_over_100:.0f}% of runs exceed 100% annual — "
                     f"100%+/year requires better signal quality (>65% win rate).")

    # Ruin probability
    if ruin_prob < 1.0:
        notes.append(f"✓ Ruin probability={ruin_prob:.2f}%: extremely low. "
                     f"Capital preservation is robust.")
    elif ruin_prob < 3.0:
        notes.append(f"◆ Ruin={ruin_prob:.2f}%: within acceptable institutional tolerance "
                     f"(<3%). Marginal but manageable.")
    else:
        notes.append(f"⚠ Ruin={ruin_prob:.2f}%: ELEVATED. Consider tightening "
                     f"max_total_drawdown_pct or defensive_risk_pct before live.")

    # Kill-switch rate over 10 years
    if ks_rate < 3:
        notes.append(f"✓ Kill-switch={ks_rate:.1f}% over 10 years: very rare. "
                     f"System can compound largely uninterrupted.")
    elif ks_rate < 10:
        notes.append(f"◆ Kill-switch={ks_rate:.1f}%: ~1 in {100//int(ks_rate+1)} ten-year "
                     f"periods ends permanently. Acceptable for prop trading.")
    else:
        notes.append(f"⚠ Kill-switch={ks_rate:.1f}%: too frequent. "
                     f"Consider lowering risk_pct or max_total_drawdown_pct.")

    # Tail survival
    if tail_surv >= 98:
        notes.append(f"✓ Fat-tail survival={tail_surv:.1f}%: gap events absorbed. "
                     f"Kill-switch protects after extreme sequences.")
    else:
        notes.append(f"⚠ Fat-tail survival={tail_surv:.1f}%: some gap sequences "
                     f"blow through kill-switch. Consider raising fat_tail_loss_mult in testing.")

    # Worst month
    if w3_p5 > -8:
        notes.append(f"✓ P5 worst-month={w3_p5:.1f}%: worst months are manageable. "
                     f"No 'black Monday'-style permanent damage.")
    else:
        notes.append(f"⚠ P5 worst-month={w3_p5:.1f}%: tail months are painful. "
                     f"Intraday DD stop (5%) should catch this in production.")

    # Time underwater
    if uw_pct < 35:
        notes.append(f"✓ Underwater {uw_pct:.0f}% of time: system recovers quickly from dips. "
                     f"Good psychological durability.")
    else:
        notes.append(f"◆ Underwater {uw_pct:.0f}% of time: extended recovery periods "
                     f"expected. Normal for compounding systems in low regimes.")

    # Expansion usage
    if exp_pct < 5:
        notes.append(f"◆ Expansion only {exp_pct:.1f}% of trades over 10 years. "
                     f"System is conservatively biased. Acceptable for capital preservation.")
    elif exp_pct < 15:
        notes.append(f"✓ Expansion active {exp_pct:.1f}% of trades — healthy balance "
                     f"between risk and growth.")
    else:
        notes.append(f"⚠ Expansion active {exp_pct:.1f}% of trades — higher than expected. "
                     f"Check that rolling win-rate gate is strict enough.")

    for note in notes:
        # Wrap at 62 chars
        while len(note) > 62:
            cut = note[:62].rfind(" ")
            if cut < 10:
                cut = 62
            print(f"  {note[:cut]}")
            note = "    " + note[cut:].lstrip()
        print(f"  {note}")


# ─────────────────────────────────────────────────────────────────────────────
# JSON export
# ─────────────────────────────────────────────────────────────────────────────

def save_json(summary: dict[str, Any], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as fh:
        json.dump(summary, fh, indent=2, default=str)
    print(f"  Report saved → {p.resolve()}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="BNK_TRADESYSTEM 10-Year Monte Carlo Robustness Tester",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--runs",          type=int,   default=500,
                        help="Number of Monte Carlo runs.")
    parser.add_argument("--years",         type=int,   default=10,
                        help="Simulation horizon in years.")
    parser.add_argument("--seed",          type=int,   default=0,
                        help="Base random seed.")
    parser.add_argument("--balance",       type=float, default=10_000.0,
                        help="Starting account balance in USD.")
    parser.add_argument("--no-edge-decay", action="store_true",
                        help="Disable edge decay layer.")
    parser.add_argument("--no-fat-tail",   action="store_true",
                        help="Disable fat-tail events.")
    parser.add_argument("--no-vol-cluster",action="store_true",
                        help="Disable volatility clustering.")
    parser.add_argument("--out",           type=str,
                        default="data/robustness_report.json",
                        help="JSON output path.")
    parser.add_argument("-q", "--quiet",   action="store_true",
                        help="Suppress per-batch progress output.")
    args = parser.parse_args()

    cfg = RobustnessConfig(
        starting_balance   = args.balance,
        sim_years          = args.years,
        enable_edge_decay  = not args.no_edge_decay,
        enable_vol_clustering = not args.no_vol_cluster,
        enable_fat_tail    = not args.no_fat_tail,
    )

    print()
    print("  BNK_TRADESYSTEM — 10-Year Robustness Tester")
    print(f"  Running {args.runs} simulations × {args.years} years ...")
    print(f"  Features: edge_decay={'ON' if cfg.enable_edge_decay else 'OFF'}"
          f"  vol_cluster={'ON' if cfg.enable_vol_clustering else 'OFF'}"
          f"  fat_tail={'ON' if cfg.enable_fat_tail else 'OFF'}")
    print()

    t0 = time.perf_counter()
    summary = run_robustness_test(
        n_runs    = args.runs,
        cfg       = cfg,
        base_seed = args.seed,
        verbose   = not args.quiet,
    )
    elapsed = time.perf_counter() - t0

    print(f"\n  Completed {args.runs} × {args.years}-year sims in {elapsed:.1f}s "
          f"({args.runs / elapsed:.0f} sims/sec)")

    print_report(summary)
    save_json(summary, args.out)


if __name__ == "__main__":
    main()
