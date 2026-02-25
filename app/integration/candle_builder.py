"""
app/integration/candle_builder.py
===================================
Converts a raw stream of (ts, bid, ask) tick events into completed OHLCV candles
at any minute-granularity (1m, 2m, 5m, 15m …).

Completely dependency-free — pure Python + stdlib only.

Usage
-----
    builder = CandleBuilder(symbol=Symbol.XAUUSD, timeframe_minutes=1)
    for tick_ts, bid, ask in tick_stream:
        completed = builder.on_tick(tick_ts, bid, ask)
        if completed:
            # feed to live provider
            push_candle(completed)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple

from ..domain.enums import Symbol
from ..domain.models import Candle


class Tick(NamedTuple):
    ts: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class CandleBuilder:
    """
    Stateful tick → OHLCV aggregator for a single (symbol, timeframe) pair.

    Parameters
    ----------
    symbol : Symbol
    timeframe_minutes : int
        Width of each candle in minutes (1, 2, 5, 15, …).
    """

    def __init__(self, symbol: Symbol, timeframe_minutes: int) -> None:
        self.symbol           = symbol
        self.tf_minutes       = timeframe_minutes
        self.tf_name          = f"{timeframe_minutes}m"

        self._open_ts:  datetime | None = None
        self._open:     float  = 0.0
        self._high:     float  = 0.0
        self._low:      float  = float("inf")
        self._close:    float  = 0.0
        self._volume:   float  = 0.0    # tick count used as proxy volume
        self._ticks:    int    = 0

        # Last completed candle (for reference)
        self.last_candle: Candle | None = None
        # Last spread seen
        self.last_spread: float = 0.0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def on_tick(self, tick: Tick) -> Candle | None:
        """
        Feed one tick.  Returns a completed Candle if the current bar has
        just closed, otherwise returns None.
        """
        self.last_spread = tick.spread
        bucket_ts = self._bucket(tick.ts)

        # First tick ever
        if self._open_ts is None:
            self._start_bar(bucket_ts, tick.mid)
            return None

        # Still in the same bar
        if bucket_ts == self._open_ts:
            self._update_bar(tick.mid)
            return None

        # Bar rolled → emit closed candle, then start fresh bar
        closed = self._emit()
        self._start_bar(bucket_ts, tick.mid)
        return closed

    def flush(self) -> Candle | None:
        """
        Force-close the current in-progress bar (e.g. on shutdown).
        Returns None if no ticks have been seen since last flush.
        """
        if self._open_ts is None or self._ticks == 0:
            return None
        return self._emit()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _bucket(self, ts: datetime) -> datetime:
        """Truncate ts down to the nearest bar boundary."""
        epoch = int(ts.timestamp())
        width = self.tf_minutes * 60
        truncated = (epoch // width) * width
        return datetime.fromtimestamp(truncated, tz=timezone.utc)

    def _start_bar(self, ts: datetime, price: float) -> None:
        self._open_ts = ts
        self._open    = price
        self._high    = price
        self._low     = price
        self._close   = price
        self._volume  = 1.0
        self._ticks   = 1

    def _update_bar(self, price: float) -> None:
        if price > self._high:
            self._high = price
        if price < self._low:
            self._low = price
        self._close  = price
        self._volume += 1.0
        self._ticks  += 1

    def _emit(self) -> Candle:
        candle = Candle(
            ts        = self._open_ts,
            symbol    = self.symbol,
            timeframe = self.tf_name,
            open      = round(self._open, 5),
            high      = round(self._high, 5),
            low       = round(self._low, 5),
            close     = round(self._close, 5),
            volume    = self._volume,
        )
        self.last_candle = candle
        return candle


class MultiTimeframeCandleBuilder:
    """
    Wraps multiple CandleBuilders for the same symbol at different timeframes.
    Single on_tick() call fans out to all timeframes.
    """

    def __init__(self, symbol: Symbol, timeframes_minutes: list[int]) -> None:
        self.symbol   = symbol
        self.builders = {
            tf: CandleBuilder(symbol, tf) for tf in timeframes_minutes
        }

    def on_tick(self, tick: Tick) -> dict[int, Candle]:
        """
        Returns a mapping of {tf_minutes: closed_candle} for any timeframes
        that just completed a bar.  Empty dict = no bars closed this tick.
        """
        closed: dict[int, Candle] = {}
        for tf, builder in self.builders.items():
            result = builder.on_tick(tick)
            if result is not None:
                closed[tf] = result
        return closed

    def last_spread(self) -> float:
        # All builders see the same ticks, grab from the 1m builder
        b = self.builders.get(1) or next(iter(self.builders.values()))
        return b.last_spread
