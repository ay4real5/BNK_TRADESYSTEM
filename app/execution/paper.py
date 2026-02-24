"""
Paper trading executor.

Simulates trade execution without touching a real broker.
Outcomes are determined by comparing the current price against SL/TP.
"""

from __future__ import annotations

from datetime import datetime

from loguru import logger

from ..domain.enums import Mode, Side, TradeOutcome
from ..domain.models import TradeIdea, TradeResult
from .base import Executor


class PaperExecutor(Executor):
    """Simulates trade execution for paper trading."""

    async def open_trade(self, idea: TradeIdea) -> TradeResult:
        trade = TradeResult(
            signal_id=idea.id,
            ts_open=datetime.utcnow(),
            symbol=idea.symbol,
            side=idea.side,
            entry=idea.entry,
            sl=idea.sl,
            tp=idea.tp,
            size=1.0,  # default paper size (1 unit)
            outcome=TradeOutcome.OPEN,
            pnl=0.0,
            mode=Mode.PAPER,
        )
        logger.info(
            "PAPER trade opened: {} {} @ {} | SL {} | TP {}",
            idea.symbol.value, idea.side.value, idea.entry, idea.sl, idea.tp,
        )
        return trade

    async def close_trade(self, trade: TradeResult, current_price: float) -> TradeResult:
        trade.ts_close = datetime.utcnow()
        risk = abs(trade.entry - trade.sl)

        if trade.side == Side.BUY:
            raw_pnl = current_price - trade.entry
        else:
            raw_pnl = trade.entry - current_price

        trade.pnl = round(raw_pnl * trade.size, 2)

        if raw_pnl > 0:
            trade.outcome = TradeOutcome.WIN
        elif raw_pnl < 0:
            trade.outcome = TradeOutcome.LOSS
        else:
            trade.outcome = TradeOutcome.BREAKEVEN

        logger.info(
            "PAPER trade closed: {} {} | PnL {} | outcome {}",
            trade.symbol.value, trade.side.value, trade.pnl, trade.outcome.value,
        )
        return trade

    async def update_trade(self, trade: TradeResult, current_price: float) -> TradeResult:
        """Check if SL or TP has been hit; if so, close the trade."""
        if trade.outcome != TradeOutcome.OPEN:
            return trade

        # Track maximum adverse excursion
        if trade.side == Side.BUY:
            adverse = trade.entry - current_price
        else:
            adverse = current_price - trade.entry
        trade.max_adverse_excursion = max(trade.max_adverse_excursion, adverse)

        # Check SL hit
        sl_hit = (
            (trade.side == Side.BUY and current_price <= trade.sl)
            or (trade.side == Side.SELL and current_price >= trade.sl)
        )
        # Check TP hit
        tp_hit = (
            (trade.side == Side.BUY and current_price >= trade.tp)
            or (trade.side == Side.SELL and current_price <= trade.tp)
        )

        if tp_hit:
            return await self.close_trade(trade, trade.tp)
        if sl_hit:
            return await self.close_trade(trade, trade.sl)

        return trade
