"""
Paper execution service.

Consumes pending signals, validates against risk engine, simulates execution,
and manages positions with SL/TP tracking.

Flow:
1. Poll DB for signals where status = "pending"
2. Validate against Risk Engine before execution
3. Simulate fill price (use current market price)
4. Create position record
5. Track SL / TP
6. Update P&L when TP or SL hit
7. Update signal status: pending → filled → closed
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from ..config import settings
from ..data import market_data, storage
from ..data.storage import DB_PATH
from ..domain.enums import LockReason, SignalStatus, TradeOutcome
from ..domain.errors import LockError
from ..domain.models import TradeIdea
from ..services import account_manager, locks, risk_manager


# ---------------------------------------------------------------------------
# Position management
# ---------------------------------------------------------------------------

async def process_pending_signals() -> dict[str, int]:
    """
    Process all pending signals: validate, fill, and create positions.
    
    Returns:
        Dictionary with counts: {executed, rejected_risk, errors}
    """
    executed = 0
    rejected = 0
    errors = 0
    
    try:
        # Get all pending signals
        pending = await storage.get_pending_signals()
        
        if not pending:
            return {"executed": 0, "rejected_risk": 0, "errors": 0}
        
        logger.debug("Processing {} pending signal(s)", len(pending))
        
        for signal in pending:
            try:
                # Validate with risk engine
                can_trade, reject_reason = await _check_risk_approval(signal)
                
                if not can_trade:
                    # Mark as rejected
                    await storage.update_signal_status(
                        signal.id,  # type: ignore
                        SignalStatus.REJECTED,
                    )
                    logger.warning(
                        "Signal #{} rejected: {}",
                        signal.id,
                        reject_reason,
                    )
                    rejected += 1
                    continue
                
                # Execute the signal
                await _execute_signal(signal)
                executed += 1
                logger.info(
                    "Executed signal #{} {} {} at ~{}",
                    signal.id,
                    signal.side.value.upper(),
                    signal.symbol.value,
                    signal.entry,
                )
                
            except Exception as exc:
                logger.error("Error processing signal #{}: {}", signal.id, exc)
                errors += 1
        
        return {
            "executed": executed,
            "rejected_risk": rejected,
            "errors": errors,
        }
        
    except Exception as exc:
        logger.error("Error in process_pending_signals: {}", exc)
        return {"executed": 0, "rejected_risk": 0, "errors": errors + 1}


async def _check_risk_approval(signal: TradeIdea) -> tuple[bool, str | None]:
    """
    Validate signal against risk engine.
    
    Returns:
        (can_trade: bool, reject_reason: str | None)
    """
    try:
        # Check risk state
        state = await locks.check_can_trade()
        
        # All checks passed
        return True, None
        
    except LockError as exc:
        # Risk check failed
        return False, str(exc)


async def _execute_signal(signal: TradeIdea) -> None:
    """
    Simulate fill and create position record.
    
    Uses current market price as fill price.
    """
    # Fetch current market price for fill simulation
    try:
        current_price = await market_data.fetch_price(signal.symbol)
    except Exception as exc:
        logger.warning(
            "Could not fetch current price for {}, using entry price: {}",
            signal.symbol.value,
            exc,
        )
        current_price = signal.entry
    
    # For paper trading, assume we get filled at current price (with some slippage simulation)
    fill_price = current_price
    
    # Calculate position size based on risk
    account = await account_manager.get_account()
    expansion_state = await storage.load_expansion_state()
    
    position_calc = risk_manager.compute_position_pnl(
        equity=account.equity,
        consecutive_losses=account.consecutive_losses,
        expansion_active=expansion_state.active,
    )
    
    # Calculate position size in lots (simplified - real would use proper lot sizing)
    risk_amount = abs(position_calc["loss"])
    sl_distance = abs(signal.entry - signal.sl)
    position_size = round(risk_amount / sl_distance, 5) if sl_distance > 0 else 0.01
    
    # Create position record (using trades table with outcome='open')
    from ..domain.models import TradeResult
    
    position = TradeResult(
        signal_id=signal.id,
        ts_open=datetime.now(timezone.utc),
        symbol=signal.symbol,
        side=signal.side,
        entry=fill_price,
        sl=signal.sl,
        tp=signal.tp,
        size=position_size,
        outcome=TradeOutcome.OPEN,
        pnl=0.0,
        mode=signal.mode,
    )
    
    # Save position
    position_id = await storage.save_trade(position)
    
    # Update signal status to EXECUTED
    await storage.update_signal_status(
        signal.id,  # type: ignore
        SignalStatus.EXECUTED,
    )
    
    # Record trade opening in risk state (increment counter)
    state = await locks.get_state()
    # Note: We don't call record_trade here because the trade hasn't closed yet
    # The counter will be updated when the position closes
    
    logger.info(
        "Position #{} opened: {} {} @ {} | SL: {} | TP: {} | Size: {}",
        position_id,
        signal.side.value.upper(),
        signal.symbol.value,
        fill_price,
        signal.sl,
        signal.tp,
        position_size,
    )


async def monitor_open_positions() -> dict[str, int]:
    """
    Monitor open positions and close them when SL or TP is hit.
    
    Returns:
        Dictionary with counts: {closed_tp, closed_sl, still_open}
    """
    closed_tp = 0
    closed_sl = 0
    still_open = 0
    
    try:
        # Get all open positions
        open_trades = await storage.get_open_trades()
        
        if not open_trades:
            return {"closed_tp": 0, "closed_sl": 0, "still_open": 0}
        
        for trade in open_trades:
            try:
                # Get current market price
                current_price = await market_data.fetch_price(trade.symbol)
                
                # Check if SL or TP hit
                hit_sl = _check_sl_hit(trade, current_price)
                hit_tp = _check_tp_hit(trade, current_price)
                
                if hit_sl:
                    await _close_position(trade, current_price, TradeOutcome.LOSS)
                    closed_sl += 1
                    logger.info(
                        "Position #{} closed at SL: {} | PnL: {:.2f}",
                        trade.id,
                        current_price,
                        trade.pnl,
                    )
                elif hit_tp:
                    await _close_position(trade, current_price, TradeOutcome.WIN)
                    closed_tp += 1
                    logger.info(
                        "Position #{} closed at TP: {} | PnL: {:.2f}",
                        trade.id,
                        current_price,
                        trade.pnl,
                    )
                else:
                    still_open += 1
                    
            except Exception as exc:
                logger.error("Error monitoring position #{}: {}", trade.id, exc)
                still_open += 1
        
        return {
            "closed_tp": closed_tp,
            "closed_sl": closed_sl,
            "still_open": still_open,
        }
        
    except Exception as exc:
        logger.error("Error in monitor_open_positions: {}", exc)
        return {"closed_tp": 0, "closed_sl": 0, "still_open": 0}


def _check_sl_hit(trade, current_price: float) -> bool:
    """Check if stop loss has been hit."""
    from ..domain.enums import Side
    
    if trade.side == Side.BUY:
        # For BUY: SL is below entry, hit when price drops to or below SL
        return current_price <= trade.sl
    else:  # SELL
        # For SELL: SL is above entry, hit when price rises to or above SL
        return current_price >= trade.sl


def _check_tp_hit(trade, current_price: float) -> bool:
    """Check if take profit has been hit."""
    from ..domain.enums import Side
    
    if trade.side == Side.BUY:
        # For BUY: TP is above entry, hit when price rises to or above TP
        return current_price >= trade.tp
    else:  # SELL
        # For SELL: TP is below entry, hit when price drops to or below TP
        return current_price <= trade.tp


async def _close_position(trade, exit_price: float, outcome: TradeOutcome) -> None:
    """
    Close a position and update all related state.
    
    Updates:
    - Trade record (set outcome, pnl, ts_close)
    - Account state (apply pnl)
    - Risk state (record trade)
    """
    from ..domain.enums import Side
    
    # Calculate P&L
    if trade.side == Side.BUY:
        pnl = (exit_price - trade.entry) * trade.size
    else:  # SELL
        pnl = (trade.entry - exit_price) * trade.size
    
    pnl = round(pnl, 2)
    
    # Update trade record
    trade.ts_close = datetime.now(timezone.utc)
    trade.outcome = outcome
    trade.pnl = pnl
    
    await storage.update_trade(trade)
    
    # Update account state
    await account_manager.apply_trade(pnl)
    
    # Update risk state
    is_loss = outcome == TradeOutcome.LOSS
    await locks.record_trade(pnl, is_loss)
    
    logger.info(
        "Position #{} closed: {} | Exit: {} | PnL: {:+.2f}",
        trade.id,
        outcome.value.upper(),
        exit_price,
        pnl,
    )


# ---------------------------------------------------------------------------
# Main execution loop
# ---------------------------------------------------------------------------

async def tick() -> dict[str, any]:
    """
    Main execution service tick: process pending signals and monitor positions.
    
    Routes signals to appropriate executor based on MODE.
    Should be called every 3-5 seconds by the scheduler.
    """
    # Import router here to avoid circular import
    from ..execution.router import process_pending_signals as router_process_signals
    
    stats = {
        "signals_executed": 0,
        "signals_rejected": 0,
        "signals_errors": 0,
        "positions_closed_tp": 0,
        "positions_closed_sl": 0,
        "positions_open": 0,
    }
    
    # Process pending signals (routed by MODE)
    signal_stats = await router_process_signals()
    stats["signals_executed"] = signal_stats["executed"]
    stats["signals_rejected"] = signal_stats["rejected_risk"]
    stats["signals_errors"] = signal_stats["errors"]
    
    # Monitor open positions (only for paper mode - demo/live use position sync)
    from ..config import settings
    from ..domain.enums import Mode
    
    if settings.mode == Mode.PAPER:
        position_stats = await monitor_open_positions()
        stats["positions_closed_tp"] = position_stats["closed_tp"]
        stats["positions_closed_sl"] = position_stats["closed_sl"]
        stats["positions_open"] = position_stats["still_open"]
    
    # Log summary if anything happened
    if any([
        stats["signals_executed"],
        stats["signals_rejected"],
        stats["positions_closed_tp"],
        stats["positions_closed_sl"],
    ]):
        logger.debug("Execution tick: {}", stats)
    
    return stats
