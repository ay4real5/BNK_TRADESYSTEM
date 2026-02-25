"""
Background trade simulator service.

Polls for PENDING signals every 10 seconds, executes them as simulated paper
trades with a 60 % win rate, persists a TradeResult, updates the signal status
to EXECUTED, and updates the daily risk state so /api/v1/status reflects the
running totals.

Enabled only when ``BNK_DEMO_ENGINE=1``.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime

from loguru import logger

from ..data import storage
from ..domain.enums import Mode, SignalStatus, TradeOutcome
from ..domain.errors import LockError
from ..domain.models import TradeResult
from . import account_manager, expansion_manager, locks, risk_manager

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIN_PROBABILITY: float = 0.60   # 60 % win rate
# PnL amounts are now computed dynamically from live equity via risk_manager.
# These module-level names are kept for test patching compatibility only.
WIN_PNL: float = 10.0
LOSS_PNL: float = -10.0
INTERVAL_SECONDS: int = 10      # polling cadence


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class TradeSimulator:
    """
    Async background service that resolves pending signals as simulated trades.

    Lifecycle::

        simulator.start()  # called on FastAPI startup
        await simulator.stop()  # called on FastAPI shutdown
    """

    def __init__(self, interval: int = INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        logger.info(
            "TradeSimulator started — processing signals every {}s (win rate {:.0%})",
            self._interval,
            WIN_PROBABILITY,
        )
        while True:
            try:
                await self._process_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("TradeSimulator error (will retry): {}", exc)
            await asyncio.sleep(self._interval)

    async def _process_pending(self) -> None:
        """Fetch all PENDING signals and resolve each one as a paper trade."""
        pending = await storage.get_pending_signals()
        if not pending:
            return

        for signal in pending:
            # Respect all risk guards before executing each simulated trade
            try:
                await locks.check_can_trade()
            except LockError as exc:
                logger.debug(
                    "TradeSimulator: skipping signal_id={} — trading locked ({})",
                    signal.id,
                    exc.reason,
                )
                continue

            is_win: bool = random.random() < WIN_PROBABILITY
            outcome: TradeOutcome = TradeOutcome.WIN if is_win else TradeOutcome.LOSS

            # Equity-based position PnL — Mode C aware:
            # expansion active → 0.9% risk; defensive → 0.5% (halved after streak)
            account = await account_manager.get_account()
            exp_state = await expansion_manager.get_state()
            sizing = risk_manager.compute_position_pnl(
                equity=account.equity,
                consecutive_losses=account.consecutive_losses,
                expansion_active=exp_state.active,
            )
            pnl: float = sizing["win"] if is_win else sizing["loss"]

            trade = TradeResult(
                signal_id=signal.id,
                ts_open=signal.ts,
                ts_close=datetime.utcnow(),
                symbol=signal.symbol,
                side=signal.side,
                entry=signal.entry,
                sl=signal.sl,
                tp=signal.tp,
                size=0.01,
                outcome=outcome,
                pnl=pnl,
                mode=Mode.PAPER,
            )

            trade_id = await storage.save_trade(trade)
            await storage.update_signal_status(signal.id, SignalStatus.EXECUTED)
            # Apply PnL to the persistent account state first,
            # then record in the daily risk state (which reads account equity).
            updated_account = await account_manager.apply_trade(pnl)
            await locks.record_trade(pnl=pnl, is_loss=not is_win)
            # Update Mode C expansion state after each settled trade
            await expansion_manager.after_trade(updated_account, is_win=is_win)

            logger.debug(
                "TradeSimulator: signal_id={} → {} | pnl={:+.2f} | risk_pct={:.2f}% "
                "| mode={} | consec_losses={} | equity=${:.2f} | dd={:.2f}% | trade_id={}",
                signal.id,
                outcome.value.upper(),
                pnl,
                sizing["risk_pct"],
                sizing["mode"],
                account.consecutive_losses,
                updated_account.equity,
                updated_account.drawdown_pct,
                trade_id,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Schedule the simulator loop as a background asyncio Task."""
        if self._task is not None:
            logger.warning("TradeSimulator.start() called but already running")
            return
        self._task = asyncio.create_task(self._run(), name="trade_simulator")

    async def stop(self) -> None:
        """Cancel the background task and wait for it to finish cleanly."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("TradeSimulator stopped")


# Module-level singleton — imported by the FastAPI lifespan
trade_simulator = TradeSimulator()
