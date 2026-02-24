"""
cTrader live execution adapter.

This is a placeholder stub. Implement the cTrader Open API WebSocket
integration when ready for live trading.
"""

from __future__ import annotations

from loguru import logger

from ..domain.errors import BrokerError
from ..domain.models import TradeIdea, TradeResult
from .base import Executor


class CTraderExecutor(Executor):
    """Live execution via the cTrader Open API."""

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
            "CTraderExecutor initialised — live order placement NOT yet implemented. "
            "Configure CTRADER_* env vars and implement the WebSocket logic."
        )

    async def _ensure_connected(self) -> None:
        if not self._connected:
            raise BrokerError("cTrader connection not established. Cannot execute live trades.")

    async def open_trade(self, idea: TradeIdea) -> TradeResult:
        await self._ensure_connected()
        # TODO: implement NewOrder via cTrader Open API
        raise NotImplementedError("CTraderExecutor.open_trade not yet implemented")

    async def close_trade(self, trade: TradeResult, current_price: float) -> TradeResult:
        await self._ensure_connected()
        raise NotImplementedError("CTraderExecutor.close_trade not yet implemented")

    async def update_trade(self, trade: TradeResult, current_price: float) -> TradeResult:
        await self._ensure_connected()
        raise NotImplementedError("CTraderExecutor.update_trade not yet implemented")
