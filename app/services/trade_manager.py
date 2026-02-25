"""
Trade manager service.

Monitors open paper/live/demo trades and marks them to market every cycle.
When SL or TP is hit the trade is closed and state is updated.

Supports mode-based routing:
- MODE=paper: PaperExecutor
- MODE=demo: CTraderExecutionService (demo environment)
- MODE=live: CTraderExecutionService (live environment)
"""

from __future__ import annotations

from loguru import logger

from ..config import settings
from ..data import market_data, storage
from ..domain.enums import Mode, TradeOutcome
from ..domain.models import TradeResult
from ..execution.paper import PaperExecutor
from ..services import locks


_paper_executor = PaperExecutor()
_ctrader_executor = None  # Lazy-loaded to avoid circular imports


def _get_ctrader_executor():
    """Lazy load cTrader executor to avoid circular imports."""
    global _ctrader_executor
    if _ctrader_executor is None:
        from ..execution.ctrader_execution import ctrader_executor
        _ctrader_executor = ctrader_executor
    return _ctrader_executor


async def tick(db_path: str = storage.DB_PATH) -> None:
    """
    Called on every scheduler tick.
    Loads all open trades and updates them against current prices.
    
    For demo/live trades, also runs position sync with broker.
    """
    open_trades = await storage.get_open_trades(db_path=db_path)
    if not open_trades:
        return

    # Run position sync for demo/live trades
    if settings.mode in [Mode.DEMO, Mode.LIVE]:
        try:
            executor = _get_ctrader_executor()
            sync_result = await executor.sync_positions()
            if sync_result["closed"] > 0 or sync_result["errors"] > 0:
                logger.info(
                    "Position sync: {} synced, {} closed, {} errors",
                    sync_result["synced"],
                    sync_result["closed"],
                    sync_result["errors"],
                )
        except Exception as exc:
            logger.error("Position sync failed: {}", exc)

    # Update mark-to-market for all open trades
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
    """
    Update a single trade with current market price.
    
    Routes to appropriate executor based on trade mode.
    """
    # Route to appropriate executor
    if trade.mode == Mode.PAPER:
        updated = await _paper_executor.update_trade(trade, current_price)
    elif trade.mode in [Mode.DEMO, Mode.LIVE]:
        executor = _get_ctrader_executor()
        updated = await executor.update_trade(trade, current_price)
    else:
        # Assist mode - no execution
        updated = trade

    # Handle trade closure
    if updated.outcome != TradeOutcome.OPEN:
        is_loss = updated.outcome == TradeOutcome.LOSS
        await storage.update_trade(updated, db_path=db_path)
        await locks.record_trade(updated.pnl, is_loss=is_loss)
        logger.info(
            "Trade {} closed: {} | PnL=${:.2f} | outcome={}",
            trade.id, trade.symbol.value, updated.pnl, updated.outcome.value,
        )
    else:
        await storage.update_trade(updated, db_path=db_path)
