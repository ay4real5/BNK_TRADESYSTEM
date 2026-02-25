"""
cTrader data provider — thin wrapper around app.integration.ctrader_data.

This module now delegates to the full async implementation in
app/integration/ctrader_data.py (CTraderLiveProvider).

For the read-only live feed (Phase 4), use:
    MARKET_DATA_SOURCE=ctrader

The server lifespan (app/api/server.py) will automatically start the
CTraderFeed and register the CTraderLiveProvider as the active DataProvider.
"""
from __future__ import annotations

from loguru import logger

from ...domain.enums import Symbol
from ...domain.models import Candle
from ..market_data import DataProvider


class CTraderDataProvider(DataProvider):
    """
    Thin compatibility shim.

    In Phase 4+ the real provider is CTraderLiveProvider (in
    app/integration/ctrader_data.py), registered automatically when
    MARKET_DATA_SOURCE=ctrader is set in .env.

    This class exists only so other code that imports CTraderDataProvider
    by name does not break.
    """

    def __init__(self, **_kwargs) -> None:
        logger.info(
            "CTraderDataProvider shim created. "
            "The live feed is managed by app.integration.ctrader_data.CTraderFeed."
        )
        self._live: DataProvider | None = None

    def _get_live(self) -> DataProvider:
        from ...integration.ctrader_data import get_feed
        feed = get_feed()
        if feed and feed.is_started:
            return feed.live_provider
        from .. import market_data
        provider = market_data._provider  # type: ignore[attr-defined]
        if provider is not None:
            return provider
        raise RuntimeError(
            "No active data provider. Is MARKET_DATA_SOURCE=ctrader set and the "
            "server running?"
        )

    async def fetch_candles(
        self,
        symbol: Symbol,
        timeframe: str,
        count: int = 300,
    ) -> list[Candle]:
        return await self._get_live().fetch_candles(symbol, timeframe, count)

    async def fetch_price(self, symbol: Symbol) -> float:
        return await self._get_live().fetch_price(symbol)

    async def fetch_spread(self, symbol: Symbol) -> float:
        return await self._get_live().fetch_spread(symbol)
