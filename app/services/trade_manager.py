"""
Trade manager service.

Monitors open paper/live trades and marks them to market every cycle.
When SL or TP is hit the trade is closed and state is updated.
"""

from __future__ import annotations

from loguru import logger

from ..data import market_data, storage
from ..domain.enums import Mode, TradeOutcome
from ..domain.models import TradeResult
from ..execution.paper import PaperExecutor
from ..services import locks


_paper_executor = PaperExecutor()


async def tick(db_path: str = storage.DB_PATH) -> None:
    """
    Called on every scheduler tick.
    Loads all open trades and updates them against current prices.
    """
    open_trades = await storage.get_open_trades(db_path=db_path)
    if not open_trades:
        return

    for trade in open_trades:
        try:
            price = await market_data.fetch_price(trade.symbol)
            await _update_single_trade(trade, price, db_path=db_path)
        except Exception as exc:
            logger.error("Error updating trade {}: {}", trade.id, exc)


async def _update_single_trade(
    trade: TradeResult,
    current_price: float,
    db_path: str = storage.DB_PATH,
) -> None:
    if trade.mode == Mode.PAPER:
        updated = await _paper_executor.update_trade(trade, current_price)
    else:
        # Live trade update — placeholder
        updated = trade

    if updated.outcome != TradeOutcome.OPEN:
        is_loss = updated.outcome == TradeOutcome.LOSS
        await storage.update_trade(updated, db_path=db_path)
        await locks.record_trade(updated.pnl, is_loss=is_loss)
        logger.info(
            "Trade {} closed: {} | PnL={} | outcome={}",
            trade.id, trade.symbol.value, updated.pnl, updated.outcome.value,
        )
    else:
        await storage.update_trade(updated, db_path=db_path)
