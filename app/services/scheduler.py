"""
APScheduler-based job scheduler.

Jobs:
  - Every 1 minute: analysis cycle (fetch → strategy → signal)
  - Every 1 minute: trade manager tick (mark-to-market open trades)
  - Daily at 22:00 UTC: end-of-day report + counter reset
"""

from __future__ import annotations

from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..data import storage
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


async def _eod_report_job() -> None:
    """End-of-day: generate report and reset daily counters."""
    try:
        summary = await storage.get_daily_summary()
        logger.info("EOD report: {}", summary)
        if _telegram_report_callback:
            await _telegram_report_callback(summary)
        # Reset state for the new day — a fresh row will be created on next load
        logger.info("Daily counters reset for new trading day")
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
