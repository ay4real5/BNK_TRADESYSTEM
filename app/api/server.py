"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from ..config import settings
from ..data.storage import init_db, init_account, init_expansion
from .routes import router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: ARG001
    """Run startup and shutdown tasks for the FastAPI application."""
    # --- startup ---
    await init_db()
    await init_account(settings.account_balance)
    await init_expansion()
    logger.info("Database initialised")

    if settings.bnk_demo_engine:
        from ..services.demo_engine import demo_engine
        from ..services.trade_simulator import trade_simulator
        demo_engine.start()
        trade_simulator.start()
        logger.info("Demo engine + TradeSimulator enabled (BNK_DEMO_ENGINE=1)")
    else:
        logger.info("Demo engine disabled (set BNK_DEMO_ENGINE=1 to enable)")

    # ── cTrader live data feed (Phase 4 — read-only) ──────────────────
    if settings.market_data_source == "ctrader":
        from ..integration.ctrader_data import CTraderFeed, set_feed
        try:
            feed = CTraderFeed.from_settings()
            set_feed(feed)
            await feed.start()
            logger.info(
                "cTrader live data feed ACTIVE (demo={}) — signals only, no orders",
                settings.ctrader_demo,
            )
        except (ValueError, ImportError) as exc:
            logger.error(
                "cTrader feed failed to start: {}. "
                "Falling back to internal data source.", exc
            )
    else:
        logger.info(
            "Market data source: internal (set MARKET_DATA_SOURCE=ctrader to use live feed)"
        )

    yield

    # --- shutdown ---
    if settings.bnk_demo_engine:
        from ..services.demo_engine import demo_engine
        from ..services.trade_simulator import trade_simulator
        await demo_engine.stop()
        await trade_simulator.stop()

    if settings.market_data_source == "ctrader":
        from ..integration.ctrader_data import get_feed
        feed = get_feed()
        if feed:
            await feed.stop()


def create_api_app() -> FastAPI:
    app = FastAPI(
        title="BNK TradeSystem API",
        description="Gold/Silver Trading Assistant REST API",
        version="0.1.0",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix="/api/v1")

    return app
