"""
Market data provider interface.

Each provider implements `fetch_candles` returning a list of Candle objects.
The `market_data` module provides a unified facade.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Protocol

import pandas as pd
from loguru import logger

from ..domain.enums import Symbol, Timeframe
from ..domain.errors import DataFetchError, InsufficientDataError
from ..domain.models import Candle


# ---------------------------------------------------------------------------
# Abstract provider interface
# ---------------------------------------------------------------------------

class DataProvider(ABC):
    """Abstract base class for market data providers."""

    @abstractmethod
    async def fetch_candles(
        self,
        symbol: Symbol,
        timeframe: str,
        count: int = 300,
    ) -> list[Candle]:
        """Return the most recent `count` candles for (symbol, timeframe)."""
        ...

    @abstractmethod
    async def fetch_price(self, symbol: Symbol) -> float:
        """Return the current mid price for symbol."""
        ...

    @abstractmethod
    async def fetch_spread(self, symbol: Symbol) -> float:
        """Return the current bid-ask spread for symbol."""
        ...


# ---------------------------------------------------------------------------
# Candle utilities
# ---------------------------------------------------------------------------

def candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    """Convert a list of Candle objects to a pandas DataFrame."""
    if not candles:
        raise InsufficientDataError("No candles provided")
    records = [c.model_dump() for c in candles]
    df = pd.DataFrame(records)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts").sort_index()
    return df


def require_min_candles(df: pd.DataFrame, minimum: int, context: str = "") -> None:
    """Raise InsufficientDataError if DataFrame has fewer rows than required."""
    if len(df) < minimum:
        msg = f"Need {minimum} candles, got {len(df)}"
        if context:
            msg = f"{context}: {msg}"
        raise InsufficientDataError(msg)


# ---------------------------------------------------------------------------
# Provider registry + facade
# ---------------------------------------------------------------------------

_provider: DataProvider | None = None


def set_provider(provider: DataProvider) -> None:
    global _provider
    _provider = provider
    logger.info("Market data provider set to {}", type(provider).__name__)


def get_provider() -> DataProvider:
    if _provider is None:
        raise DataFetchError("No market data provider registered. Call set_provider() first.")
    return _provider


async def fetch_candles(symbol: Symbol, timeframe: str, count: int = 300) -> list[Candle]:
    return await get_provider().fetch_candles(symbol, timeframe, count)


async def fetch_price(symbol: Symbol) -> float:
    return await get_provider().fetch_price(symbol)


async def fetch_spread(symbol: Symbol) -> float:
    return await get_provider().fetch_spread(symbol)
