"""
Async SQLite storage layer using aiosqlite.

All DB interactions go through this module so the rest of the app stays
independent of the underlying database engine.
"""

from __future__ import annotations

import json
from datetime import datetime, date
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from ..domain.enums import Bias, Mode, Side, SignalStatus, Symbol, TradeOutcome, LockReason
from ..domain.errors import StorageError
from ..domain.models import AccountState, ExpansionState, RiskState, TradeIdea, TradeResult


DB_PATH = "data/trading.db"
MIGRATIONS_PATH = Path(__file__).parent / "migrations.sql"


async def init_db(db_path: str = DB_PATH) -> None:
    """Initialise the database by running migrations.sql."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    sql = MIGRATIONS_PATH.read_text()
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(sql)
        # Live-migration: add new columns to account table if upgrading from older schema
        for col, col_def in [
            ("equity_at_day_start", "REAL NOT NULL DEFAULT 10000.0"),
            ("consecutive_losses", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE account ADD COLUMN {col} {col_def}")
                await db.commit()
                logger.debug("Migrated account table: added column {}", col)
            except Exception:  # column already exists
                pass
        await db.commit()
    logger.info("Database initialised at {}", db_path)

# ---------------------------------------------------------------------------
# Signal (TradeIdea) storage
# ---------------------------------------------------------------------------

async def save_signal(idea: TradeIdea, db_path: str = DB_PATH) -> int:
    """Insert a TradeIdea and return its new row ID."""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            INSERT INTO signals (ts, symbol, side, entry, sl, tp, score, reasons_json, mode, status, bias)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idea.ts.isoformat(),
                idea.symbol.value,
                idea.side.value,
                idea.entry,
                idea.sl,
                idea.tp,
                idea.score,
                json.dumps(idea.reasons),
                idea.mode.value,
                idea.status.value,
                idea.bias.value,
            ),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def update_signal_status(
    signal_id: int,
    status: SignalStatus,
    db_path: str = DB_PATH,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE signals SET status = ? WHERE id = ?",
            (status.value, signal_id),
        )
        await db.commit()


async def get_recent_signals(
    limit: int = 10,
    db_path: str = DB_PATH,
) -> list[TradeIdea]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
    return [_row_to_idea(r) for r in rows]


async def get_pending_signals(db_path: str = DB_PATH) -> list[TradeIdea]:
    """Return all signals whose status is PENDING (not yet simulated or actioned)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM signals WHERE status = ? ORDER BY ts ASC",
            (SignalStatus.PENDING.value,),
        )
        rows = await cur.fetchall()
    return [_row_to_idea(r) for r in rows]


async def get_signal_by_id(signal_id: int, db_path: str = DB_PATH) -> TradeIdea | None:
    """Return a single signal by its primary key, or None if not found."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM signals WHERE id = ?",
            (signal_id,),
        )
        row = await cur.fetchone()
    return _row_to_idea(row) if row else None


def _row_to_idea(row: aiosqlite.Row) -> TradeIdea:
    return TradeIdea(
        id=row["id"],
        ts=datetime.fromisoformat(row["ts"]),
        symbol=Symbol(row["symbol"]),
        side=Side(row["side"]),
        entry=row["entry"],
        sl=row["sl"],
        tp=row["tp"],
        score=row["score"],
        reasons=json.loads(row["reasons_json"]),
        mode=Mode(row["mode"]),
        status=SignalStatus(row["status"]),
        bias=Bias(row["bias"]),
    )


# ---------------------------------------------------------------------------
# Trade storage
# ---------------------------------------------------------------------------

async def save_trade(trade: TradeResult, db_path: str = DB_PATH) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            INSERT INTO trades (
                signal_id, ts_open, ts_close, symbol, side, entry, sl, tp,
                size, outcome, pnl, mode, mae,
                broker_position_id, broker_order_id,
                execution_latency_ms, entry_slippage,
                spread_at_entry, exit_price, exit_slippage
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.signal_id,
                trade.ts_open.isoformat(),
                trade.ts_close.isoformat() if trade.ts_close else None,
                trade.symbol.value,
                trade.side.value,
                trade.entry,
                trade.sl,
                trade.tp,
                trade.size,
                trade.outcome.value,
                trade.pnl,
                trade.mode.value,
                trade.max_adverse_excursion,
                trade.broker_position_id,
                trade.broker_order_id,
                trade.execution_latency_ms,
                trade.entry_slippage,
                trade.spread_at_entry,
                trade.exit_price,
                trade.exit_slippage,
            ),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def update_trade(trade: TradeResult, db_path: str = DB_PATH) -> None:
    if trade.id is None:
        raise StorageError("Cannot update trade without an ID")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE trades
            SET ts_close = ?, outcome = ?, pnl = ?, mae = ?,
                exit_price = ?, exit_slippage = ?
            WHERE id = ?
            """,
            (
                trade.ts_close.isoformat() if trade.ts_close else None,
                trade.outcome.value,
                trade.pnl,
                trade.max_adverse_excursion,
                trade.exit_price,
                trade.exit_slippage,
                trade.id,
            ),
        )
        await db.commit()


async def get_open_trades(db_path: str = DB_PATH) -> list[TradeResult]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM trades WHERE outcome = 'open' ORDER BY ts_open"
        )
        rows = await cur.fetchall()
    return [_row_to_trade(r) for r in rows]


async def get_today_trades(db_path: str = DB_PATH) -> list[TradeResult]:
    today = date.today().isoformat()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM trades WHERE ts_open LIKE ? ORDER BY ts_open",
            (f"{today}%",),
        )
        rows = await cur.fetchall()
    return [_row_to_trade(r) for r in rows]


def _row_to_trade(row: aiosqlite.Row) -> TradeResult:
    keys = row.keys() if hasattr(row, 'keys') else []
    def _col(name: str, default=None):
        try:
            return row[name]
        except (IndexError, KeyError):
            return default

    return TradeResult(
        id=row["id"],
        signal_id=row["signal_id"],
        ts_open=datetime.fromisoformat(row["ts_open"]),
        ts_close=datetime.fromisoformat(row["ts_close"]) if row["ts_close"] else None,
        symbol=Symbol(row["symbol"]),
        side=Side(row["side"]),
        entry=row["entry"],
        sl=row["sl"],
        tp=row["tp"],
        size=row["size"],
        outcome=TradeOutcome(row["outcome"]),
        pnl=row["pnl"],
        mode=Mode(row["mode"]),
        max_adverse_excursion=row["mae"],
        broker_position_id=_col("broker_position_id"),
        broker_order_id=_col("broker_order_id"),
        execution_latency_ms=_col("execution_latency_ms"),
        entry_slippage=_col("entry_slippage"),
        spread_at_entry=_col("spread_at_entry"),
        exit_price=_col("exit_price"),
        exit_slippage=_col("exit_slippage"),
    )


async def get_trade_by_id(trade_id: int, db_path: str = DB_PATH) -> TradeResult | None:
    """Fetch a single trade by its local DB id (full journal fields included)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        row = await cur.fetchone()
    return _row_to_trade(row) if row else None


# ---------------------------------------------------------------------------
# State storage
# ---------------------------------------------------------------------------

async def load_risk_state(db_path: str = DB_PATH) -> RiskState:
    today = date.today().isoformat()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM state WHERE date = ?", (today,))
        row = await cur.fetchone()
    if row is None:
        return RiskState(date=today)
    return RiskState(
        date=row["date"],
        trades_count=row["trades_count"],
        losses_count=row["losses_count"],
        pnl=row["pnl"],
        drawdown_pct=row["drawdown_pct"],
        locked_until_ts=datetime.fromisoformat(row["locked_until_ts"]) if row["locked_until_ts"] else None,
        lock_reason=LockReason(row["lock_reason"]) if row["lock_reason"] else None,
        paused_until_ts=datetime.fromisoformat(row["paused_until_ts"]) if row["paused_until_ts"] else None,
        kill_switch=bool(row["kill_switch"]),
    )


async def save_risk_state(state: RiskState, db_path: str = DB_PATH) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO state (date, trades_count, losses_count, pnl, drawdown_pct,
                               locked_until_ts, lock_reason, paused_until_ts, kill_switch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                trades_count    = excluded.trades_count,
                losses_count    = excluded.losses_count,
                pnl             = excluded.pnl,
                drawdown_pct    = excluded.drawdown_pct,
                locked_until_ts = excluded.locked_until_ts,
                lock_reason     = excluded.lock_reason,
                paused_until_ts = excluded.paused_until_ts,
                kill_switch     = excluded.kill_switch
            """,
            (
                state.date,
                state.trades_count,
                state.losses_count,
                state.pnl,
                state.drawdown_pct,
                state.locked_until_ts.isoformat() if state.locked_until_ts else None,
                state.lock_reason.value if state.lock_reason else None,
                state.paused_until_ts.isoformat() if state.paused_until_ts else None,
                int(state.kill_switch),
            ),
        )
        await db.commit()


async def get_daily_summary(db_path: str = DB_PATH) -> dict[str, Any]:
    """Aggregate trade statistics for today."""
    today = date.today().isoformat()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,
                SUM(pnl) AS total_pnl
            FROM trades
            WHERE ts_open LIKE ? AND outcome != 'open'
            """,
            (f"{today}%",),
        )
        row = await cur.fetchone()
    if row is None:
        return {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
    return {
        "total": row["total"] or 0,
        "wins": row["wins"] or 0,
        "losses": row["losses"] or 0,
        "total_pnl": row["total_pnl"] or 0.0,
    }


# ---------------------------------------------------------------------------
# Account state storage
# ---------------------------------------------------------------------------

async def init_account(starting_balance: float, db_path: str = DB_PATH) -> None:
    """Seed the single-row account table if it does not yet exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO account
                (id, starting_balance, balance, equity, peak_equity,
                 equity_at_day_start, total_pnl, drawdown_pct, consecutive_losses)
            VALUES (1, ?, ?, ?, ?, ?, 0.0, 0.0, 0)
            """,
            (starting_balance, starting_balance, starting_balance,
             starting_balance, starting_balance),
        )
        await db.commit()
    logger.debug("Account initialised with starting balance ${:,.2f}", starting_balance)


async def load_account_state(db_path: str = DB_PATH) -> AccountState:
    """Load the persistent account row (creating default if missing)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM account WHERE id = 1")
        row = await cur.fetchone()
    if row is None:
        return AccountState()
    return AccountState(
        starting_balance=row["starting_balance"],
        balance=row["balance"],
        equity=row["equity"],
        peak_equity=row["peak_equity"],
        equity_at_day_start=row["equity_at_day_start"],
        total_pnl=row["total_pnl"],
        drawdown_pct=row["drawdown_pct"],
        consecutive_losses=row["consecutive_losses"],
        last_updated=datetime.fromisoformat(row["last_updated"]) if row["last_updated"] else None,
    )


async def save_account_state(state: AccountState, db_path: str = DB_PATH) -> None:
    """Persist the account row (upsert)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO account
                (id, starting_balance, balance, equity, peak_equity,
                 equity_at_day_start, total_pnl, drawdown_pct, consecutive_losses, last_updated)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                starting_balance    = excluded.starting_balance,
                balance             = excluded.balance,
                equity              = excluded.equity,
                peak_equity         = excluded.peak_equity,
                equity_at_day_start = excluded.equity_at_day_start,
                total_pnl           = excluded.total_pnl,
                drawdown_pct        = excluded.drawdown_pct,
                consecutive_losses  = excluded.consecutive_losses,
                last_updated        = excluded.last_updated
            """,
            (
                state.starting_balance,
                state.balance,
                state.equity,
                state.peak_equity,
                state.equity_at_day_start,
                state.total_pnl,
                state.drawdown_pct,
                state.consecutive_losses,
                state.last_updated.isoformat() if state.last_updated else None,
            ),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Rolling trade statistics  (Mode C expansion gate)
# ---------------------------------------------------------------------------

async def get_rolling_trade_stats(n: int = 30, db_path: str = DB_PATH) -> dict[str, Any]:
    """
    Return statistics for the last ``n`` closed (non-open) trades.

    Computes:
    - ``total``            : number of trades in window
    - ``wins`` / ``losses``: counts
    - ``win_rate``         : wins / total  (0.0 if no trades)
    - ``max_dd_pct``       : maximum peak-to-trough drawdown % across the window
                             (computed from sequential PnL, not equity curve)
    - ``pnls``             : ordered list of individual PnL values (oldest first)
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT outcome, pnl
            FROM trades
            WHERE outcome != 'open'
            ORDER BY ts_open DESC
            LIMIT ?
            """,
            (n,),
        )
        rows = await cur.fetchall()

    if not rows:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "max_dd_pct": 0.0, "pnls": []}

    # Rows are newest-first; reverse for chronological order
    pnls = [r["pnl"] for r in reversed(rows)]
    wins = sum(1 for r in rows if r["outcome"] == "win")
    losses = sum(1 for r in rows if r["outcome"] == "loss")
    total = len(rows)
    win_rate = round(wins / total, 4) if total > 0 else 0.0

    # Max peak-to-trough drawdown across sequential PnL window
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        running += p
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    # Express max_dd as % of the peak equity level at that point
    # Use simple ratio: max_dd / peak * 100 (or 0 if peak == 0)
    max_dd_pct = round(max_dd / peak * 100, 4) if peak > 0 else 0.0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "max_dd_pct": max_dd_pct,
        "pnls": pnls,
    }


# ---------------------------------------------------------------------------
# Expansion state storage  (Mode C)
# ---------------------------------------------------------------------------

async def init_expansion(db_path: str = DB_PATH) -> None:
    """Seed the expansion state row if it does not yet exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO expansion (id) VALUES (1)"
        )
        await db.commit()


async def load_expansion_state(db_path: str = DB_PATH) -> ExpansionState:
    """Load the single-row expansion state (returns fresh default if missing)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM expansion WHERE id = 1")
        row = await cur.fetchone()
    if row is None:
        return ExpansionState()
    return ExpansionState(
        active=bool(row["active"]),
        start_equity=row["start_equity"],
        trades_in_window=row["trades_in_window"],
        consecutive_losses=row["consecutive_losses"],
        activated_at=datetime.fromisoformat(row["activated_at"]) if row["activated_at"] else None,
        exit_reason=row["exit_reason"],
        atr_spike_active=bool(row["atr_spike_active"]),
    )


async def save_expansion_state(state: ExpansionState, db_path: str = DB_PATH) -> None:
    """Persist the expansion state row (upsert)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO expansion
                (id, active, start_equity, trades_in_window, consecutive_losses,
                 activated_at, exit_reason, atr_spike_active)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                active             = excluded.active,
                start_equity       = excluded.start_equity,
                trades_in_window   = excluded.trades_in_window,
                consecutive_losses = excluded.consecutive_losses,
                activated_at       = excluded.activated_at,
                exit_reason        = excluded.exit_reason,
                atr_spike_active   = excluded.atr_spike_active
            """,
            (
                int(state.active),
                state.start_equity,
                state.trades_in_window,
                state.consecutive_losses,
                state.activated_at.isoformat() if state.activated_at else None,
                state.exit_reason,
                int(state.atr_spike_active),
            ),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# cTrader live feed: ticks + candles persistence
# ---------------------------------------------------------------------------

async def save_tick(
    symbol: str,
    ts: datetime,
    bid: float,
    ask: float,
    db_path: str = DB_PATH,
) -> None:
    """Insert a raw tick into the ticks table."""
    mid    = (bid + ask) / 2.0
    spread = ask - bid
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO ticks (ts, symbol, bid, ask, mid, spread) VALUES (?,?,?,?,?,?)",
            (ts.isoformat(), symbol, bid, ask, round(mid, 6), round(spread, 6)),
        )
        await db.commit()


async def save_candle(
    table: str,   # "candles_m1" or "candles_m5"
    symbol: str,
    ts_open: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    tick_count: int,
    db_path: str = DB_PATH,
) -> None:
    """Upsert a candle row (INSERT OR REPLACE by symbol+ts_open)."""
    if table not in ("candles_m1", "candles_m5"):
        raise StorageError(f"Invalid candle table: {table}")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"""
            INSERT INTO {table} (symbol, ts_open, open, high, low, close, tick_count)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(symbol, ts_open) DO UPDATE SET
                high       = MAX(excluded.high, {table}.high),
                low        = MIN(excluded.low,  {table}.low),
                close      = excluded.close,
                tick_count = excluded.tick_count
            """,
            (symbol, ts_open.isoformat(), open_, high, low, close, tick_count),
        )
        await db.commit()


async def get_ticks(
    symbol: str,
    limit: int = 200,
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return the most recent `limit` ticks for a symbol."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ts, symbol, bid, ask, mid, spread FROM ticks "
            "WHERE symbol=? ORDER BY ts DESC LIMIT ?",
            (symbol, limit),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]


async def get_candles(
    table: str,   # "candles_m1" or "candles_m5"
    symbol: str,
    limit: int = 200,
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return the most recent `limit` candles from a candle table."""
    if table not in ("candles_m1", "candles_m5"):
        raise StorageError(f"Invalid candle table: {table}")
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT symbol, ts_open, open, high, low, close, tick_count "
            f"FROM {table} WHERE symbol=? ORDER BY ts_open DESC LIMIT ?",
            (symbol, limit),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]

