"""
CSV-based OHLCV data provider — useful for backtesting and development.

Expected CSV format:
  ts,open,high,low,close,volume
  2024-01-02 07:00:00,2063.5,2065.1,2062.0,2064.3,1200

Place CSV files at: data/csv/<SYMBOL>_<TIMEFRAME>.csv
e.g.  data/csv/XAUUSD_15m.csv
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from loguru import logger

from ...domain.enums import Symbol
from ...domain.errors import DataFetchError
from ...domain.models import Candle
from ..market_data import DataProvider


class CSVDataProvider(DataProvider):
    """Load historical OHLCV data from local CSV files."""

    def __init__(self, data_dir: str = "data/csv") -> None:
        self.data_dir = Path(data_dir)
        self._cache: dict[str, pd.DataFrame] = {}

    def _load(self, symbol: Symbol, timeframe: str) -> pd.DataFrame:
        key = f"{symbol.value}_{timeframe}"
        if key in self._cache:
            return self._cache[key]

        path = self.data_dir / f"{key}.csv"
        if not path.exists():
            raise DataFetchError(f"CSV file not found: {path}")

        df = pd.read_csv(path, parse_dates=["ts"])
        df = df.set_index("ts").sort_index()
        required = {"open", "high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise DataFetchError(f"CSV missing columns: {missing}")

        if "volume" not in df.columns:
            df["volume"] = 0.0

        self._cache[key] = df
        logger.info("Loaded {} candles from {}", len(df), path)
        return df

    async def fetch_candles(
        self,
        symbol: Symbol,
        timeframe: str,
        count: int = 300,
    ) -> list[Candle]:
        df = self._load(symbol, timeframe)
        tail = df.tail(count)
        candles = []
        for ts, row in tail.iterrows():
            candles.append(
                Candle(
                    ts=ts,
                    symbol=symbol,
                    timeframe=timeframe,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0.0)),
                )
            )
        return candles

    async def fetch_price(self, symbol: Symbol) -> float:
        # Return the last close from the most recent candle available
        for tf in ("15m", "1h", "1d"):
            try:
                df = self._load(symbol, tf)
                return float(df["close"].iloc[-1])
            except DataFetchError:
                continue
        raise DataFetchError(f"No CSV data available for price lookup: {symbol}")

    async def fetch_spread(self, symbol: Symbol) -> float:
        # Return a realistic mock spread
        spreads = {Symbol.XAUUSD: 0.25, Symbol.XAGUSD: 0.03}
        return spreads.get(symbol, 0.25)


class SyntheticDataProvider(DataProvider):
    """
    Generates synthetic price data for testing when no CSV is available.
    Produces a random-walk OHLCV series seeded for reproducibility.
    """

    SEED_PRICES = {Symbol.XAUUSD: 2050.0, Symbol.XAGUSD: 23.50}

    def __init__(self, seed: int = 42) -> None:
        self._seed = seed

    async def fetch_candles(
        self,
        symbol: Symbol,
        timeframe: str,
        count: int = 300,
    ) -> list[Candle]:
        import random as _r
        rng = _r.Random(self._seed)
        base = self.SEED_PRICES.get(symbol, 1000.0)
        candles = []
        price = base
        minutes_per_bar = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        bar_min = minutes_per_bar.get(timeframe, 15)
        now = datetime(2024, 1, 1, 0, 0)

        for i in range(count):
            change = rng.gauss(0, base * 0.001)
            o = price
            c = price + change
            h = max(o, c) + abs(rng.gauss(0, base * 0.0005))
            l = min(o, c) - abs(rng.gauss(0, base * 0.0005))
            v = rng.uniform(500, 2000)
            ts = now + timedelta(minutes=bar_min * i)
            candles.append(
                Candle(
                    ts=ts,
                    symbol=symbol,
                    timeframe=timeframe,
                    open=round(o, 4),
                    high=round(h, 4),
                    low=round(l, 4),
                    close=round(c, 4),
                    volume=round(v, 2),
                )
            )
            price = c

        return candles

    async def fetch_price(self, symbol: Symbol) -> float:
        candles = await self.fetch_candles(symbol, "15m", count=1)
        return candles[-1].close

    async def fetch_spread(self, symbol: Symbol) -> float:
        return {Symbol.XAUUSD: 0.25, Symbol.XAGUSD: 0.03}.get(symbol, 0.25)
