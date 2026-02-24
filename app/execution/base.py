"""Abstract Executor interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..domain.models import TradeIdea, TradeResult


class Executor(ABC):
    """Base interface for all execution adapters."""

    @abstractmethod
    async def open_trade(self, idea: TradeIdea) -> TradeResult:
        """Execute the trade idea and return a TradeResult."""
        ...

    @abstractmethod
    async def close_trade(self, trade: TradeResult, current_price: float) -> TradeResult:
        """Close an open trade at the given price."""
        ...

    @abstractmethod
    async def update_trade(self, trade: TradeResult, current_price: float) -> TradeResult:
        """Mark-to-market an open trade (update PnL + MAE)."""
        ...
