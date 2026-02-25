"""
Main entrypoint for the BNK Gold/Silver Trading System.

Startup sequence:
  1. Initialise logging
  2. Initialise database
  3. Register market data provider
  4. Build Telegram bot
  5. Start background scheduler
  6. Run FastAPI + Telegram bot concurrently
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import uvicorn
from loguru import logger

from .api.server import create_api_app
from .config import settings
from .data import storage
from .data.providers.ohlc_csv import SyntheticDataProvider
from .data import market_data
from .logging_config import setup_logging
from .services import analyzer, scheduler
from .telegram import bot


async def _run_telegram(application) -> None:
    """Run the Telegram bot using polling."""
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot polling started")
    # Keep running until stopped
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def _run_api(api_app) -> None:
    """Run the FastAPI server."""
    config = uvicorn.Config(
        api_app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    logger.info("API server starting on {}:{}", settings.api_host, settings.api_port)
    await server.serve()


async def main_async() -> None:
    # 1. Logging
    setup_logging(settings.log_level)

    # 2. Database
    await storage.init_db()

    # 3. Market data provider
    # Development/testing: use SyntheticDataProvider
    # Production: replace with CTraderDataProvider or CSVDataProvider
    provider = SyntheticDataProvider(seed=42)
    market_data.set_provider(provider)
    logger.info("Using SyntheticDataProvider — replace with a real provider for live trading")

    # 4. Telegram bot
    application = bot.build_application()

    # Inject notify callbacks
    analyzer.set_telegram_notify(bot.notify_signal)
    scheduler.set_report_callback(bot.send_eod_report)

    # 5. Scheduler
    scheduler.start_scheduler()

    # 6. Run API + Telegram concurrently
    api_app = create_api_app()

    # Temporarily disable Telegram for OAuth testing
    # await asyncio.gather(
    #     _run_telegram(application),
    #     _run_api(api_app),
    # )
    await _run_api(api_app)


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Shutting down — bye!")
        scheduler.stop_scheduler()
        sys.exit(0)


if __name__ == "__main__":
    main()
