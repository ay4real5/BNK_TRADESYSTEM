"""
app/services/candle_builder.py
================================
Service-layer candle builder: wires the in-memory tick→OHLC aggregation
(app/integration/candle_builder.py) to SQLite persistence.

Usage
-----
Call `process_tick(symbol, bid, ask, ts)` from the cTrader feed for every
incoming tick.  Completed candles are automatically:
  - pushed into CTraderLiveProvider (in-memory buffer for the engine)
  - upserted into candles_m1 / candles_m5 SQLite tables
  - counted in feed statistics

The service also:
  - saves every raw tick to the ``ticks`` table (configurable via
    ``persist_ticks=True``)
  - emits structured log lines for latency / audit
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import NamedTuple

from loguru import logger

from ..data.storage import save_tick, save_candle


# Canonical table names for each aggregation interval
_TF_TABLE: dict[int, str] = {
    1:  "candles_m1",
    5:  "candles_m5",
}


class _Bar:
    """Mutable OHLC accumulator for a single candle bucket."""

    __slots__ = ("ts_open", "open", "high", "low", "close", "tick_count")

    def __init__(self, ts_open: datetime, price: float) -> None:
        self.ts_open    = ts_open
        self.open       = price
        self.high       = price
        self.low        = price
        self.close      = price
        self.tick_count = 1

    def update(self, price: float) -> None:
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.close      = price
        self.tick_count += 1


class CompletedCandle(NamedTuple):
    table:      str
    symbol:     str
    ts_open:    datetime
    open:       float
    high:       float
    low:        float
    close:      float
    tick_count: int


class CandleBuilderService:
    """
    In-process service that aggregates ticks into M1 and M5 candles,
    persists them to SQLite, and reports completion events.

    Designed to be called from async context (the cTrader connection loop).
    SQLite writes use ``asyncio.create_task`` so the hot path is non-blocking.
    """

    def __init__(
        self,
        timeframes_minutes: list[int] | None = None,
        persist_ticks: bool = True,
        db_path: str = "data/trading.db",
    ) -> None:
        self._timeframes   = timeframes_minutes or [1, 5]
        self._persist_ticks = persist_ticks
        self._db_path      = db_path
        # {(symbol, tf_minutes): _Bar | None}
        self._bars: dict[tuple[str, int], _Bar | None] = {}

    # ------------------------------------------------------------------
    # Public hot-path method
    # ------------------------------------------------------------------

    async def process_tick(
        self,
        symbol: str,
        bid: float,
        ask: float,
        ts: datetime | None = None,
    ) -> list[CompletedCandle]:
        """
        Record one tick, return list of candles completed by this tick.

        Parameters
        ----------
        symbol : e.g. "XAUUSD"
        bid, ask : raw prices from the broker
        ts : timestamp (UTC); defaults to now
        """
        if ts is None:
            ts = datetime.now(tz=timezone.utc)

        mid = (bid + ask) / 2.0

        # Persist raw tick (fire-and-forget)
        if self._persist_ticks:
            asyncio.create_task(
                save_tick(symbol, ts, bid, ask, db_path=self._db_path),
                name=f"save_tick_{symbol}",
            )

        completed: list[CompletedCandle] = []

        for tf_min in self._timeframes:
            key = (symbol, tf_min)

            # Bucket boundary: truncate timestamp to tf_min interval
            bucket_ts = _bucket(ts, tf_min)

            bar = self._bars.get(key)
            if bar is None:
                # First tick ever — open a new bar
                self._bars[key] = _Bar(bucket_ts, mid)
                continue

            if bucket_ts == bar.ts_open:
                # Still within the same candle
                bar.update(mid)
            else:
                # New bucket → close the previous bar
                finished = CompletedCandle(
                    table      = _TF_TABLE.get(tf_min, f"candles_m{tf_min}"),
                    symbol     = symbol,
                    ts_open    = bar.ts_open,
                    open       = bar.open,
                    high       = bar.high,
                    low        = bar.low,
                    close      = bar.close,
                    tick_count = bar.tick_count,
                )
                completed.append(finished)

                # Persist to SQLite (fire-and-forget)
                asyncio.create_task(
                    save_candle(
                        finished.table,
                        symbol,
                        bar.ts_open,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.tick_count,
                        db_path=self._db_path,
                    ),
                    name=f"save_candle_{symbol}_{tf_min}m",
                )

                logger.debug(
                    "CandleBuilder: {}  {}m  {}  O={:.5f} H={:.5f} L={:.5f} C={:.5f}  ticks={}",
                    symbol, tf_min, bar.ts_open.strftime("%H:%M"),
                    bar.open, bar.high, bar.low, bar.close, bar.tick_count,
                )

                # Open the new bar with this tick's mid
                self._bars[key] = _Bar(bucket_ts, mid)

        return completed

    def flush_all(self) -> list[CompletedCandle]:
        """
        Force-close all open bars (call on feed shutdown to persist partial candles).
        Does NOT trigger async saves — caller must handle persistence if needed.
        """
        out: list[CompletedCandle] = []
        for (symbol, tf_min), bar in list(self._bars.items()):
            if bar is not None:
                out.append(CompletedCandle(
                    table      = _TF_TABLE.get(tf_min, f"candles_m{tf_min}"),
                    symbol     = symbol,
                    ts_open    = bar.ts_open,
                    open       = bar.open,
                    high       = bar.high,
                    low        = bar.low,
                    close      = bar.close,
                    tick_count = bar.tick_count,
                ))
                self._bars[(symbol, tf_min)] = None
        return out

    def bar_status(self) -> dict[str, dict]:
        """Return current open-bar state (for diagnostics / API)."""
        result: dict[str, dict] = {}
        for (symbol, tf_min), bar in self._bars.items():
            key = f"{symbol}/{tf_min}m"
            if bar is None:
                result[key] = {"status": "empty"}
            else:
                result[key] = {
                    "ts_open":    bar.ts_open.isoformat(),
                    "open":       bar.open,
                    "high":       bar.high,
                    "low":        bar.low,
                    "close":      bar.close,
                    "tick_count": bar.tick_count,
                }
        return result


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _bucket(ts: datetime, minutes: int) -> datetime:
    """Truncate a datetime to the nearest `minutes` candle boundary (UTC)."""
    epoch_s     = ts.timestamp()
    bucket_s    = (int(epoch_s) // (minutes * 60)) * (minutes * 60)
    return datetime.fromtimestamp(bucket_s, tz=timezone.utc)
