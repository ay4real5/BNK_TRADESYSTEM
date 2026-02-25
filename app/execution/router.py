"""
Execution router - routes signals to appropriate execution service based on MODE.

MODE routing:
- assist: No execution (signals only)
- paper: PaperExecutor (simulated fills)
- demo: CTraderExecutionService (demo environment)
- live: CTraderExecutionService (live environment) - NOT YET ENABLED

This ensures clean separation and swappable execution backends.
"""

from __future__ import annotations

from loguru import logger

from ..config import settings
from ..data import storage
from ..domain.enums import Mode, SignalStatus
from ..domain.errors import ExecutionError, LockError
from ..domain.models import TradeIdea
from ..execution.paper_execution import _check_risk_approval, _execute_signal as _execute_paper_signal


# Lazy-load executors to avoid circular imports
_ctrader_executor = None


def _get_ctrader_executor():
    """Lazy load cTrader executor."""
    global _ctrader_executor
    if _ctrader_executor is None:
        from ..execution.ctrader_execution import ctrader_executor
        _ctrader_executor = ctrader_executor
    return _ctrader_executor


async def process_pending_signals() -> dict[str, int]:
    """
    Process all pending signals using mode-appropriate executor.
    
    Routes to:
    - MODE=paper: Paper execution (simulation)
    - MODE=demo: cTrader demo execution
    - MODE=live: cTrader live execution (when enabled)
    - MODE=assist: No execution (skip)
    
    Returns:
        Dictionary with counts: {executed, rejected_risk, errors}
    """
    executed = 0
    rejected = 0
    errors = 0
    
    # Skip execution in assist mode
    if settings.mode == Mode.ASSIST:
        return {"executed": 0, "rejected_risk": 0, "errors": 0}
    
    try:
        # Get all pending signals
        pending = await storage.get_pending_signals()
        
        if not pending:
            return {"executed": 0, "rejected_risk": 0, "errors": 0}
        
        logger.debug(
            "Processing {} pending signal(s) in MODE={}",
            len(pending),
            settings.mode.value,
        )
        
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
                
                # Route to appropriate executor
                await _execute_signal_with_routing(signal)
                executed += 1
                
                logger.info(
                    "✅ Executed signal #{} {} {} at {} (MODE={})",
                    signal.id,
                    signal.side.value.upper(),
                    signal.symbol.value,
                    signal.entry,
                    settings.mode.value,
                )
                
            except Exception as exc:
                logger.error("❌ Error processing signal #{}: {}", signal.id, exc)
                errors += 1
        
        return {
            "executed": executed,
            "rejected_risk": rejected,
            "errors": errors,
        }
        
    except Exception as exc:
        logger.error("Error in process_pending_signals: {}", exc)
        return {"executed": 0, "rejected_risk": 0, "errors": errors + 1}


async def _execute_signal_with_routing(signal: TradeIdea) -> None:
    """
    Execute signal using mode-appropriate executor.
    
    Args:
        signal: Approved signal ready for execution
    
    Raises:
        ExecutionError: If execution fails
    """
    mode = settings.mode
    
    if mode == Mode.PAPER:
        # Use paper execution (simulation)
        await _execute_paper_signal(signal)
        
    elif mode in [Mode.DEMO, Mode.LIVE]:
        # Use cTrader execution service
        executor = _get_ctrader_executor()
        
        try:
            trade = await executor.open_trade(signal)
            logger.success(
                "✅ cTrader position opened: #{} | {} {} @ {} | SL: {} | TP: {}",
                trade.id,
                signal.side.value.upper(),
                signal.symbol.value,
                trade.entry,
                trade.sl,
                trade.tp,
            )
        except Exception as exc:
            logger.error("❌ cTrader execution failed: {}", exc)
            # Mark signal as rejected
            if signal.id:
                await storage.update_signal_status(signal.id, SignalStatus.REJECTED)
            raise ExecutionError(f"cTrader execution failed: {exc}")
    
    else:
        # ASSIST mode - should not reach here
        logger.warning("Signal execution skipped in ASSIST mode")
