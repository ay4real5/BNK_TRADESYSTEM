"""
cTrader data provider — fetches live OHLCV data via the cTrader Open API.

This is a stub implementation. Fill in authentication and WebSocket
logic once CTRADER_CLIENT_ID / CTRADER_ACCESS_TOKEN are configured.
"""

from __future__ import annotations

from loguru import logger

from ...domain.enums import Symbol
from ...domain.errors import BrokerError
from ...domain.models import Candle
from ..market_data import DataProvider


class CTraderDataProvider(DataProvider):
    """Live market data from cTrader Open API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        access_token: str,
        account_id: str,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id = account_id
        self._connected = False
        logger.warning(
            "CTraderDataProvider initialised — live connection NOT yet implemented. "
            "Use CSVDataProvider or SyntheticDataProvider in the meantime."
        )

    async def _ensure_connected(self) -> None:
        if not self._connected:
            raise BrokerError(
                "cTrader WebSocket connection not implemented yet. "
                "Please implement the connection logic or use another provider."
            )

    async def fetch_candles(
        self,
        symbol: Symbol,
        timeframe: str,
        count: int = 300,
    ) -> list[Candle]:
        await self._ensure_connected()
        # TODO: implement cTrader GetTrendbars request
        raise NotImplementedError("CTraderDataProvider.fetch_candles not yet implemented")

    async def fetch_price(self, symbol: Symbol) -> float:
        await self._ensure_connected()
        raise NotImplementedError("CTraderDataProvider.fetch_price not yet implemented")

    async def fetch_spread(self, symbol: Symbol) -> float:
        await self._ensure_connected()
        raise NotImplementedError("CTraderDataProvider.fetch_spread not yet implemented")
