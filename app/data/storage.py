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
from ..domain.models import RiskState, TradeIdea, TradeResult


DB_PATH = "data/trading.db"
MIGRATIONS_PATH = Path(__file__).parent / "migrations.sql"


async def init_db(db_path: str = DB_PATH) -> None:
    """Initialise the database by running migrations.sql."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    sql = MIGRATIONS_PATH.read_text()
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(sql)
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
            INSERT INTO trades (signal_id, ts_open, ts_close, symbol, side, entry, sl, tp, size, outcome, pnl, mode, mae)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            SET ts_close = ?, outcome = ?, pnl = ?, mae = ?
            WHERE id = ?
            """,
            (
                trade.ts_close.isoformat() if trade.ts_close else None,
                trade.outcome.value,
                trade.pnl,
                trade.max_adverse_excursion,
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
    )


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
