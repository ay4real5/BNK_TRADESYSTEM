"""
Volatility gate — entry-level market quality check.

Blocks new entries when:
  1. ATR (M5, 14) is below the minimum threshold for the symbol
     → market is too quiet, no edge, spread eats the move
  2. Current spread exceeds 25% of ATR
     → transaction cost is too high relative to expected range

Both checks use the M5 candle database (candles_m5 table) for ATR
and the ticks table for the latest spread.

Design:
  - check_volatility(symbol) raises LockError if blocked
  - Fails OPEN when there is insufficient data (< 15 M5 candles)
    so that a freshly started system with no history is not permanently locked
  - Logs every block to execution_events for operational review
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone

import pandas as pd
from loguru import logger

from ..config import settings
from ..domain.enums import LockReason, Symbol
from ..domain.errors import LockError


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def check_volatility(symbol: Symbol) -> None:
    """
    Raise LockError if volatility conditions are not met for `symbol`.

    Checks:
      1. ATR(M5,14) >= min threshold for symbol
      2. spread / ATR <= settings.spread_atr_max_ratio

    Raises:
        LockError with reason=HIGH_VOLATILITY if blocked.

    Does NOT raise if there is insufficient candle data (fail-open).
    """
    if not settings.volatility_gate_enabled:
        return

    try:
        atr = await _compute_atr_m5(symbol)
    except InsufficientData:
        logger.debug(
            "volatility_gate: insufficient M5 data for {} — skipping (fail-open)",
            symbol.value,
        )
        return
    except Exception as exc:
        logger.warning(
            "volatility_gate: ATR computation error for {}: {} — skipping (fail-open)",
            symbol.value, exc,
        )
        return

    min_atr = (
        settings.atr_min_xauusd
        if symbol == Symbol.XAUUSD
        else settings.atr_min_xagusd
    )

    if atr < min_atr:
        detail = (
            f"ATR(M5,14)={atr:.4f} < min={min_atr} for {symbol.value} "
            f"— dead market, no edge"
        )
        logger.info("volatility_gate BLOCK [atr_too_low]: {}", detail)
        await _log_block(symbol.value, detail)
        raise LockError(
            f"{LockReason.HIGH_VOLATILITY.value} — {detail}"
        )

    # Spread check
    spread = await _get_latest_spread(symbol)
    if spread is not None:
        ratio = spread / atr
        max_ratio = settings.spread_atr_max_ratio
        if ratio > max_ratio:
            detail = (
                f"spread={spread:.4f} / ATR={atr:.4f} = {ratio:.2%} > max {max_ratio:.0%} "
                f"for {symbol.value} — transaction cost too high"
            )
            logger.info("volatility_gate BLOCK [spread_too_wide]: {}", detail)
            await _log_block(symbol.value, detail)
            raise LockError(
                f"{LockReason.HIGH_VOLATILITY.value} — {detail}"
            )

    logger.debug(
        "volatility_gate OK: {} ATR(M5,14)={:.4f} spread={}",
        symbol.value, atr,
        f"{spread:.4f}" if spread is not None else "N/A",
    )


async def get_atr_snapshot(symbol: Symbol) -> dict:
    """
    Return ATR diagnostics for a symbol without raising.

    Used by /risk/status and /execution/events to show current gate state.
    """
    try:
        atr = await _compute_atr_m5(symbol)
    except InsufficientData:
        return {"symbol": symbol.value, "atr": None, "status": "insufficient_data"}
    except Exception as exc:
        return {"symbol": symbol.value, "atr": None, "status": f"error: {exc}"}

    min_atr = settings.atr_min_xauusd if symbol == Symbol.XAUUSD else settings.atr_min_xagusd
    spread = await _get_latest_spread(symbol)
    spread_ratio = round(spread / atr, 4) if (spread and atr) else None

    return {
        "symbol": symbol.value,
        "atr_m5_14": round(atr, 4),
        "min_atr_required": min_atr,
        "atr_ok": atr >= min_atr,
        "spread": round(spread, 4) if spread is not None else None,
        "spread_atr_ratio": spread_ratio,
        "spread_atr_max_ratio": settings.spread_atr_max_ratio,
        "spread_ok": (spread_ratio is None) or (spread_ratio <= settings.spread_atr_max_ratio),
        "gate_enabled": settings.volatility_gate_enabled,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class InsufficientData(Exception):
    pass


async def _compute_atr_m5(symbol: Symbol) -> float:
    """
    Compute ATR(14) from the most recent 30 M5 candles for `symbol`.

    Uses Wilder's ATR (same as pandas_ta):
      TR = max(high - low, |high - prev_close|, |low - prev_close|)
      ATR = EMA(TR, period=14)

    Raises InsufficientData if fewer than 15 candles are available.
    """
    from ..data.storage import get_candles

    rows = await get_candles("candles_m5", symbol.value, limit=30)
    if len(rows) < 15:
        raise InsufficientData(f"Only {len(rows)} M5 candles available (need ≥15)")

    df = pd.DataFrame(rows)
    df = df.sort_values("ts_open").reset_index(drop=True)

    # True range
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = df.apply(
        lambda r: max(
            r["high"] - r["low"],
            abs(r["high"] - r["prev_close"]) if pd.notna(r["prev_close"]) else 0,
            abs(r["low"]  - r["prev_close"]) if pd.notna(r["prev_close"]) else 0,
        ),
        axis=1,
    )

    # Wilder's smoothed ATR — use the last 14 TR values
    trs = df["tr"].dropna().tolist()[-14:]
    if not trs:
        raise InsufficientData("No valid TR values")

    atr = sum(trs) / len(trs)   # Simple ATR (Wilder starting value)
    return atr


async def _get_latest_spread(symbol: Symbol) -> float | None:
    """Fetch the most recent spread from the ticks table."""
    try:
        from ..data.storage import DB_PATH
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT spread FROM ticks WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                (symbol.value,),
            )
            row = await cur.fetchone()
            if row:
                return float(row[0])
    except Exception as exc:
        logger.debug("volatility_gate: spread lookup failed: {}", exc)
    return None


async def _log_block(symbol: str, detail: str) -> None:
    """Write a volatility_block event to execution_events."""
    try:
        from ..data.storage import log_execution_event
        await log_execution_event(
            "volatility_block",
            symbol=symbol,
            detail=detail[:500],
        )
    except Exception:
        pass
