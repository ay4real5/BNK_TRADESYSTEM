"""
APScheduler-based job scheduler.

Jobs:
  - Every 1 minute: analysis cycle (fetch → strategy → signal)
  - Every 1 minute: trade manager tick (mark-to-market open trades)
  - Every N seconds (demo): auto-execution loop (score/risk filter → cTrader)
  - Every N seconds (demo/live): position sync (broker reconcile → DB)
  - Daily at 22:00 UTC: end-of-day report + counter reset
"""

from __future__ import annotations

from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..config import settings
from ..data import storage
from ..domain.enums import Mode, SignalStatus
from ..domain.models import RiskState
from ..services import analyzer, trade_manager


_scheduler: AsyncIOScheduler | None = None
_telegram_report_callback = None


def set_report_callback(callback) -> None:
    """Inject the end-of-day Telegram report callback."""
    global _telegram_report_callback
    _telegram_report_callback = callback


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


async def _analysis_job() -> None:
    try:
        ideas = await analyzer.run_analysis_cycle()
        if ideas:
            logger.info("Analysis cycle: {} setup(s) found", len(ideas))
    except Exception as exc:
        logger.error("Analysis job error: {}", exc)


async def _trade_manager_job() -> None:
    try:
        await trade_manager.tick()
    except Exception as exc:
        logger.error("Trade manager job error: {}", exc)


async def _paper_execution_job() -> None:
    """Paper execution: process pending signals and monitor positions."""
    try:
        from ..execution import paper_execution
        stats = await paper_execution.tick()
        if any([
            stats.get("signals_executed", 0),
            stats.get("positions_closed_tp", 0),
            stats.get("positions_closed_sl", 0),
        ]):
            logger.debug("Paper execution: {}", stats)
    except Exception as exc:
        logger.error("Paper execution job error: {}", exc)


async def _demo_execution_job() -> None:
    """
    Autonomous demo execution loop.

    Runs every AUTO_EXECUTE_INTERVAL_SEC seconds when:
      - MODE=demo
      - AUTO_EXECUTE_DEMO=1

    For each pending signal (sorted best score first):
      1. Score gate: signal.score >= MIN_SCORE_TO_EXECUTE
      2. Session gate: current UTC hour is inside London or NY session
      3. One-position-per-symbol gate: no demo/live open trade for same symbol
      4. Risk engine gate: locks.check_can_trade() passes
      5. Send to cTrader executor with full safeguards
    """
    if not settings.auto_execute_demo:
        return

    try:
        from datetime import datetime, timezone
        from ..domain.enums import Symbol
        from ..execution.ctrader_execution import ctrader_executor
        from ..services import locks
        from ..domain.errors import LockError

        pending = await storage.get_pending_signals()
        if not pending:
            return

        # Sort best score first
        candidates = sorted(pending, key=lambda s: s.score, reverse=True)

        # Score gate — drop anything below minimum threshold immediately
        candidates = [s for s in candidates if s.score >= settings.min_score_to_execute]
        if not candidates:
            return

        # Load demo/live open trades for the symbol conflict check
        # (Session gate is enforced inside locks.check_can_trade — no duplicate needed here)
        open_trades = await storage.get_open_trades()
        open_demo_symbols: set[Symbol] = {
            t.symbol for t in open_trades
            if t.mode in (Mode.DEMO,)
        }

        executed = 0
        for signal in candidates:
            # One-position-per-symbol gate
            if signal.symbol in open_demo_symbols:
                logger.debug(
                    "Auto-exec: {} already has an open demo position, skipping signal #{}",
                    signal.symbol.value, signal.id,
                )
                continue

            # Volatility gate (per-symbol, fail-open on missing data)
            try:
                from ..services.volatility_gate import check_volatility
                await check_volatility(signal.symbol)
            except LockError as vol_err:
                logger.info(
                    "Auto-exec: volatility gate blocked {} — {}",
                    signal.symbol.value, vol_err.reason,
                )
                continue  # try next candidate; vol gate is per-symbol
            except Exception:
                pass  # fail-open

            # Risk engine gate
            try:
                await locks.check_can_trade()
            except LockError as lock_err:
                logger.info("Auto-exec: risk locked — {}", lock_err.reason)
                # Telegram alert — once per lock reason (fire-and-forget)
                try:
                    from ..telegram.bot import notify_risk_locked
                    await notify_risk_locked(str(lock_err.reason))
                except Exception:
                    pass
                break  # risk is global; no point checking remaining signals

            # Execute via cTrader (bypass_risk=False → full safeguard re-check inside)
            try:
                trade = await ctrader_executor.open_trade(signal)
                open_demo_symbols.add(signal.symbol)  # prevent double-fire in same tick
                executed += 1
                logger.success(
                    "🤖 AUTO-EXECUTED: signal #{} {} {} @ {} | score={:.1f}",
                    signal.id,
                    signal.side.value.upper(),
                    signal.symbol.value,
                    trade.entry,
                    signal.score,
                )
                # Telegram alert — fire-and-forget
                try:
                    from ..telegram.bot import notify_trade_opened
                    await notify_trade_opened(trade, signal_score=signal.score)
                except Exception:
                    pass
            except Exception as exc:
                logger.error("Auto-exec failed for signal #{}: {}", signal.id, exc)
                # Mark signal rejected so it doesn't keep retrying forever
                if signal.id:
                    await storage.update_signal_status(signal.id, SignalStatus.REJECTED)

        if executed:
            logger.info("Auto-execution tick: {} order(s) sent to cTrader", executed)

    except Exception as exc:
        logger.error("Demo execution job error: {}", exc)


async def _position_sync_job() -> None:
    """
    Periodic broker reconciliation.

    Polls cTrader for open positions every POSITION_SYNC_INTERVAL_SEC.
    When a position is missing from broker (SL/TP hit or manual close):
      - Fetches final PnL from deal history
      - Updates local DB trade to WIN/LOSS
      - Feeds result into risk engine counters (daily loss, drawdown)
    """
    try:
        from ..execution.ctrader_execution import ctrader_executor
        result = await ctrader_executor.sync_positions()
        if result["closed"] > 0:
            logger.info(
                "🔄 Position sync: {} broker-closed position(s) recorded | errors={}",
                result["closed"], result["errors"],
            )
            # Telegram: one alert per closed trade
            for closed_trade in result.get("closed_trades", []):
                try:
                    from ..telegram.bot import notify_trade_closed
                    await notify_trade_closed(closed_trade)
                except Exception:
                    pass
            if result.get("error_details"):
                for d in result["error_details"]:
                    logger.warning("  sync detail: {}", d)
        elif result["errors"] > 0:
            logger.warning("Position sync errors: {}", result.get("error_details", []))
    except Exception as exc:
        logger.error("Position sync job error: {}", exc)


async def _eod_report_job() -> None:
    """End-of-day: generate report and snapshot equity_at_day_start for the new day."""
    try:
        summary = await storage.get_daily_summary()
        logger.info("EOD report: {}", summary)
        if _telegram_report_callback:
            await _telegram_report_callback(summary)

        # Snapshot today's closing equity as tomorrow's day-start equity.
        # This is what the intraday drawdown stop uses for its reference point.
        from ..services import account_manager as _am
        from ..data.storage import save_account_state
        account = await _am.get_account()
        updated = account.model_copy(update={"equity_at_day_start": account.equity})
        await save_account_state(updated)
        logger.info(
            "EOD: equity_at_day_start set to ${:.2f} for next trading day",
            account.equity,
        )
        # Note: daily risk counters (losses_count, trades_count, pnl) reset
        # automatically — load_risk_state() queries by today's date and returns
        # a fresh RiskState if no row exists for the new date.
    except Exception as exc:
        logger.error("EOD report job error: {}", exc)


def start_scheduler() -> None:
    sched = get_scheduler()

    # Analysis cycle — every 60 seconds
    sched.add_job(
        _analysis_job,
        trigger=IntervalTrigger(seconds=60),
        id="analysis",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Trade manager — every 60 seconds
    sched.add_job(
        _trade_manager_job,
        trigger=IntervalTrigger(seconds=60),
        id="trade_manager",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Paper execution — every 5 seconds (only in PAPER mode)
    if settings.mode == Mode.PAPER:
        sched.add_job(
            _paper_execution_job,
            trigger=IntervalTrigger(seconds=5),
            id="paper_execution",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("Paper execution service enabled (every 5s)")

    # Demo autonomous execution — every N seconds (only in DEMO mode + AUTO_EXECUTE_DEMO=1)
    if settings.mode == Mode.DEMO and settings.auto_execute_demo:
        sched.add_job(
            _demo_execution_job,
            trigger=IntervalTrigger(seconds=settings.auto_execute_interval_sec),
            id="demo_execution",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "🤖 Autonomous demo execution ENABLED | score≥{} | every {}s",
            settings.min_score_to_execute,
            settings.auto_execute_interval_sec,
        )
    elif settings.mode == Mode.DEMO:
        logger.info(
            "Demo mode active but AUTO_EXECUTE_DEMO=0 — set it to 1 in .env to enable autonomous execution"
        )

    # Position sync — every N seconds in demo/live mode
    if settings.mode in (Mode.DEMO, Mode.LIVE):
        sched.add_job(
            _position_sync_job,
            trigger=IntervalTrigger(seconds=settings.position_sync_interval_sec),
            id="position_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "🔄 Position sync enabled (broker reconcile every {}s)",
            settings.position_sync_interval_sec,
        )

    # EOD report — 22:00 UTC daily
    sched.add_job(
        _eod_report_job,
        trigger=CronTrigger(hour=22, minute=0, timezone="UTC"),
        id="eod_report",
        replace_existing=True,
    )

    sched.start()
    logger.info("Scheduler started — analysis every 60s, EOD report at 22:00 UTC")


def stop_scheduler() -> None:
    sched = get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
        logger.info("Scheduler stopped")
