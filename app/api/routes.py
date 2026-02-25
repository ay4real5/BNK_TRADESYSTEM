"""FastAPI route definitions."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from loguru import logger

from ..config import settings
from ..data import storage
from ..services import account_manager, expansion_manager, locks, risk_manager
from ..services.ctrader_oauth import oauth_service
from ..data.storage import get_daily_summary

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "mode": settings.mode.value}


# ---------------------------------------------------------------------------
# Data-source endpoints
# ---------------------------------------------------------------------------

@router.get("/data-source")
async def data_source() -> dict:
    """
    Returns the active market data source and live feed diagnostics.
    Response shape is stable — UI and smoke tests depend on it.
    """
    source = settings.market_data_source.upper()
    # Normalise legacy values
    if source == "INTERNAL":
        source = "INTERNAL_DEMO"
    if source == "CTRADER":
        source = "CTRADER_LIVE"

    symbols = [s.value for s in settings.active_symbols]

    if source == "CTRADER_LIVE":
        from ..integration.ctrader_data import get_feed
        feed = get_feed()
        if feed and feed.is_started:
            s = feed.stats
            last_tick_ts = s.get("last_tick_ts")
            # Estimate latency from last tick age
            latency_ms: float | None = None
            from ..domain.enums import Symbol
            for sym in settings.active_symbols:
                age = feed.live_provider.last_tick_age_seconds(sym)
                if age is not None:
                    latency_ms = round(age * 1000, 1)
                    break
            return {
                "source":        source,
                "feed_status":   "connected",
                "connected":     True,
                "last_tick_ts":  last_tick_ts,
                "latency_ms":    latency_ms,
                "symbols":       symbols,
                "ticks_received":    s.get("ticks_received", 0),
                "candles_completed": s.get("candles_completed", 0),
                "reconnects":        s.get("reconnects", 0),
                "connected_since":   s.get("connected_since"),
                "candle_buffer_counts": s.get("candle_buffer_counts", {}),
            }
        else:
            return {
                "source":       source,
                "feed_status":  "disconnected",
                "connected":    False,
                "last_tick_ts": None,
                "latency_ms":   None,
                "symbols":      symbols,
            }
    else:
        # INTERNAL_DEMO — always "connected", no real tick timestamps
        return {
            "source":       source,
            "feed_status":  "connected",
            "connected":    True,
            "last_tick_ts": None,
            "latency_ms":   None,
            "symbols":      symbols,
        }


@router.post("/data-source/{source_name}")
async def switch_data_source(source_name: str) -> dict:
    """
    Switch the active market data source at runtime.

    Valid values: INTERNAL_DEMO, CTRADER_LIVE

    Switching to CTRADER_LIVE requires credentials in .env.
    Switching to INTERNAL_DEMO stops any running cTrader feed and restores
    the synthetic/demo data provider.
    """
    name_upper = source_name.upper()
    if name_upper not in ("INTERNAL_DEMO", "CTRADER_LIVE"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source '{source_name}'. Valid: INTERNAL_DEMO, CTRADER_LIVE",
        )

    # Tear down the existing cTrader feed if running
    from ..integration.ctrader_data import get_feed, set_feed
    existing_feed = get_feed()
    if existing_feed and existing_feed.is_started:
        try:
            await existing_feed.stop()
        except Exception:
            pass
        set_feed(None)  # type: ignore[arg-type]

    if name_upper == "CTRADER_LIVE":
        from ..integration.ctrader_data import CTraderFeed
        from ..data.market_data import set_provider
        try:
            feed = CTraderFeed.from_settings()
            set_feed(feed)
            await feed.start()
            settings.market_data_source = "ctrader"
            return {
                "source":      "CTRADER_LIVE",
                "feed_status": "connecting",
                "success":     True,
                "message":     "cTrader feed starting — allow 30–60 s for warm-up",
            }
        except (ValueError, ImportError) as exc:
            settings.market_data_source = "internal"
            raise HTTPException(
                status_code=503,
                detail=f"cTrader feed failed to start: {exc}",
            ) from exc
    else:
        # Restore synthetic provider
        from ..data.providers.ohlc_csv import SyntheticDataProvider
        from ..data.market_data import set_provider
        try:
            set_provider(SyntheticDataProvider())
        except Exception:
            pass  # Synthetic provider may not exist — internal mode still works
        settings.market_data_source = "internal"
        return {
            "source":      "INTERNAL_DEMO",
            "feed_status": "connected",
            "success":     True,
            "message":     "Switched to internal/demo data source",
        }


# ---------------------------------------------------------------------------
# Candles + ticks (cTrader audit trail)
# ---------------------------------------------------------------------------

@router.get("/candles")
async def get_candles(
    symbol: str = Query("XAUUSD", description="Symbol e.g. XAUUSD"),
    tf: str     = Query("m1",     description="Timeframe: m1 or m5"),
    limit: int  = Query(200,      ge=1, le=1000),
) -> list[dict]:
    """
    Return persisted OHLC candles from SQLite (written by the cTrader feed).
    Works with both INTERNAL_DEMO (empty) and CTRADER_LIVE.
    """
    tf_lower = tf.lower()
    table_map = {"m1": "candles_m1", "m5": "candles_m5"}
    table = table_map.get(tf_lower)
    if table is None:
        raise HTTPException(status_code=400, detail=f"Unknown timeframe '{tf}'. Use m1 or m5.")
    rows = await storage.get_candles(table, symbol.upper(), limit=limit)
    return rows


@router.get("/ticks")
async def get_ticks(
    symbol: str = Query("XAUUSD", description="Symbol e.g. XAUUSD"),
    limit:  int = Query(200,      ge=1, le=1000),
) -> list[dict]:
    """
    Return raw ticks persisted by the cTrader feed.
    """
    rows = await storage.get_ticks(symbol.upper(), limit=limit)
    return rows


# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------

@router.get("/status")
async def status() -> dict:
    state, summary, account, exp = await asyncio.gather(
        locks.get_state(),
        get_daily_summary(),
        account_manager.get_account(),
        expansion_manager.get_state(),
    )
    return {
        "mode": settings.mode.value,
        "risk_mode": "expansion" if exp.active else "defensive",
        "is_locked": state.is_locked,
        "kill_switch": state.kill_switch,
        "lock_reason": state.lock_reason.value if state.lock_reason else None,
        "trades_today": summary["total"],
        "wins_today": summary["wins"],
        "losses_today": summary["losses"],
        "pnl_today": round(summary["total_pnl"] or 0.0, 2),
        "drawdown_pct": state.drawdown_pct,
        "daily_loss_limit": risk_manager.compute_daily_loss_limit(account.equity),
        "account_balance": account.balance,
        "equity": account.equity,
        "max_trades_per_day": settings.max_trades_per_day,
        "expansion_trades_remaining": (
            settings.expansion_max_trades - exp.trades_in_window
        ) if exp.active else None,
    }


@router.get("/account")
async def account_state() -> dict:
    account, exp = await asyncio.gather(
        account_manager.get_account(),
        expansion_manager.get_state(),
    )
    sizing = risk_manager.compute_position_pnl(
        equity=account.equity,
        consecutive_losses=account.consecutive_losses,
        expansion_active=exp.active,
    )
    return {
        "starting_balance": account.starting_balance,
        "balance": account.balance,
        "equity": account.equity,
        "peak_equity": account.peak_equity,
        "total_pnl": account.total_pnl,
        "drawdown_pct": account.drawdown_pct,
        "consecutive_losses": account.consecutive_losses,
        "equity_at_day_start": account.equity_at_day_start,
        "last_updated": account.last_updated.isoformat() if account.last_updated else None,
        # Risk limits
        "daily_loss_limit": risk_manager.compute_daily_loss_limit(account.equity),
        "daily_loss_limit_desc": risk_manager.daily_loss_limit_str(account.equity),
        "intraday_dd_limit": risk_manager.compute_intraday_dd_limit(account.equity_at_day_start),
        "intraday_dd_limit_desc": risk_manager.intraday_dd_limit_str(account.equity_at_day_start),
        "total_drawdown_limit": risk_manager.compute_max_drawdown_limit(account.peak_equity),
        "total_drawdown_limit_desc": risk_manager.total_drawdown_limit_str(account.peak_equity),
        # Mode C
        "risk_mode": "expansion" if exp.active else "defensive",
        "effective_risk_pct": sizing["risk_pct"],
        "next_trade_win": sizing["win"],
        "next_trade_loss": sizing["loss"],
        "expansion_active": exp.active,
        "expansion_trades_in_window": exp.trades_in_window,
        "expansion_trades_remaining": (
            settings.expansion_max_trades - exp.trades_in_window
        ) if exp.active else None,
        "expansion_start_equity": exp.start_equity if exp.active else None,
        "expansion_exit_reason": exp.exit_reason,
        # Config
        "risk_per_trade_pct": settings.defensive_risk_pct,
        "expansion_risk_pct": settings.expansion_risk_pct,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "max_total_drawdown_pct": settings.max_total_drawdown_pct,
        "intraday_dd_stop_pct": settings.intraday_dd_stop_pct,
        "consecutive_loss_threshold": settings.consecutive_loss_threshold,
        "consecutive_loss_scale_factor": settings.consecutive_loss_scale_factor,
    }


@router.get("/signals/recent")
async def recent_signals(limit: int = 10) -> list[dict]:
    signals = await storage.get_recent_signals(limit=min(limit, 50))
    return [
        {
            "id": s.id,
            "ts": s.ts.isoformat(),
            "symbol": s.symbol.value,
            "side": s.side.value,
            "entry": s.entry,
            "sl": s.sl,
            "tp": s.tp,
            "score": s.score,
            "status": s.status.value,
            "bias": s.bias.value,
        }
        for s in signals
    ]


@router.post("/signals/{signal_id}/execute")
async def execute_signal(
    signal_id: int,
    force: bool = Query(False, description="Skip risk checks (for manual test trades only)"),
) -> dict:
    """
    Manually trigger cTrader execution for a single signal.

    Works only in demo/live mode.

    Guards enforced:
    - Signal must not already be EXECUTED (idempotency).
    - Signal must not already have an open trade in the DB (idempotency).
    - No other open position for the same symbol is allowed (prevents unintentional hedging).
      Pass ?force=true to bypass the symbol-conflict check for deliberate manual tests.

    Add ?force=true to also bypass today's risk lock.
    """
    from ..domain.enums import Mode, SignalStatus
    from ..execution.router import _execute_signal_with_routing

    # 1. Load signal
    signal = await storage.get_signal_by_id(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail=f"Signal {signal_id} not found")

    # 2. Guard: must be in demo or live mode
    if settings.mode not in (Mode.DEMO, Mode.LIVE):
        raise HTTPException(
            status_code=400,
            detail=f"Execution only available in demo/live mode (current: {settings.mode.value}). "
                   "POST /api/v1/mode/demo first.",
        )

    # 3a. Idempotency: refuse to re-execute a signal that already has an open trade
    open_trades = await storage.get_open_trades()
    existing = [t for t in open_trades if t.signal_id == signal_id]
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Signal {signal_id} already has an open position (trade_id={existing[0].id}). "
                   "Close it before re-executing.",
        )

    # 3b. Idempotency: refuse to re-execute a signal already marked EXECUTED
    #     (unless its trade was closed and the status wasn't reset — rare but defensive)
    if signal.status == SignalStatus.EXECUTED:
        raise HTTPException(
            status_code=409,
            detail=f"Signal {signal_id} is already marked EXECUTED. "
                   "Use a new/pending signal instead.",
        )

    # 3c. One-position-per-symbol: block if a demo/live trade for the same symbol is open.
    #     Paper simulation trades are intentionally excluded — they run independently.
    #     Prevents unintentional hedging (buy+sell on XAUUSD at the same time).
    #     Override with ?force=true only for deliberate manual tests.
    live_open = [
        t for t in open_trades
        if t.symbol == signal.symbol and t.mode in (Mode.DEMO, Mode.LIVE)
    ]
    if live_open and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A {live_open[0].side.value} position is already open for "
                f"{signal.symbol.value} (trade_id={live_open[0].id}). "
                "Close it first, or add ?force=true to override (manual test only)."
            ),
        )

    # 4. If force=true, wipe today's risk state so the executor can proceed
    if force:
        from ..data.storage import load_risk_state, save_risk_state
        from ..domain.models import RiskState
        state = await load_risk_state()
        unlocked = RiskState(
            date=state.date,
            trades_count=0,           # clear trade counter
            losses_count=0,           # clear loss counter
            pnl=0.0,                  # clear PnL
            drawdown_pct=0.0,
            locked_until_ts=None,
            lock_reason=None,
            paused_until_ts=None,
            kill_switch=False,
        )
        await save_risk_state(unlocked)
        logger.warning(
            "⚠️  FORCE EXECUTE: risk state reset for signal #{} | was pnl={} losses={} trades={}",
            signal_id, state.pnl, state.losses_count, state.trades_count,
        )

    # 4. Normalise status to PENDING so the executor won't skip it
    if signal.status not in (SignalStatus.PENDING,):
        await storage.update_signal_status(signal_id, SignalStatus.PENDING)
        signal = await storage.get_signal_by_id(signal_id)

    # 5. Execute
    try:
        if force and settings.mode in (Mode.DEMO, Mode.LIVE):
            # Bypass router's risk re-check — go straight to the cTrader executor
            from ..execution.ctrader_execution import ctrader_executor
            await ctrader_executor.open_trade(signal, bypass_risk=True)  # type: ignore[arg-type]
        else:
            await _execute_signal_with_routing(signal)  # type: ignore[arg-type]
        return {
            "success": True,
            "signal_id": signal_id,
            "symbol": signal.symbol.value,  # type: ignore[union-attr]
            "side": signal.side.value,  # type: ignore[union-attr]
            "mode": settings.mode.value,
            "forced": force,
            "message": "Order submitted to cTrader. Check /api/v1/execution/sync-positions.",
        }
    except Exception as exc:
        logger.error("execute_signal endpoint error: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/report/today")
async def today_report() -> dict:
    return await storage.get_daily_summary()


@router.get("/mode")
async def get_mode() -> dict:
    return {"mode": settings.mode.value}


@router.post("/mode/{mode_name}")
async def set_mode(mode_name: str) -> dict:
    from ..domain.enums import Mode
    from ..data.storage import log_execution_event
    try:
        prev = settings.mode.value
        settings.mode = Mode(mode_name.lower())
        await log_execution_event("mode_change", detail=f"{prev} → {settings.mode.value}")
        return {"mode": settings.mode.value, "success": True}
    except ValueError:
        return {"error": f"Invalid mode: {mode_name}", "success": False}


@router.get("/auto-execute/status")
async def auto_execute_status() -> dict:
    """Current autonomous execution configuration."""
    from ..domain.enums import Mode
    from ..services.scheduler import get_scheduler
    sched = get_scheduler()
    job = sched.get_job("demo_execution") if sched.running else None
    return {
        "auto_execute_demo": settings.auto_execute_demo,
        "min_score_to_execute": settings.min_score_to_execute,
        "auto_execute_interval_sec": settings.auto_execute_interval_sec,
        "position_sync_interval_sec": settings.position_sync_interval_sec,
        "mode": settings.mode.value,
        "scheduler_job_active": job is not None,
        "in_trading_session": _is_trading_session(),
    }


def _is_trading_session() -> bool:
    """True if current UTC hour is inside London or NY session."""
    from datetime import datetime, timezone
    hour = datetime.now(timezone.utc).hour
    in_london = settings.london_open_utc <= hour < settings.london_close_utc
    in_ny     = settings.ny_open_utc     <= hour < settings.ny_close_utc
    return in_london or in_ny


@router.post("/auto-execute/enable")
async def enable_auto_execute() -> dict:
    """Enable autonomous demo execution at runtime (no restart needed)."""
    from ..domain.enums import Mode
    if settings.mode not in (Mode.DEMO, Mode.LIVE):
        raise HTTPException(
            status_code=400,
            detail=f"Auto-execute only available in demo/live mode (current: {settings.mode.value})",
        )
    settings.auto_execute_demo = True
    # Start the scheduler job if scheduler is running
    from ..services.scheduler import get_scheduler, _demo_execution_job
    sched = get_scheduler()
    if sched.running:
        from apscheduler.triggers.interval import IntervalTrigger
        sched.add_job(
            _demo_execution_job,
            trigger=IntervalTrigger(seconds=settings.auto_execute_interval_sec),
            id="demo_execution",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    logger.warning("🤖 Auto-execution ENABLED via API | score≥{}", settings.min_score_to_execute)
    return {
        "success": True,
        "auto_execute_demo": True,
        "min_score_to_execute": settings.min_score_to_execute,
        "message": f"System will now auto-execute signals with score≥{settings.min_score_to_execute} during London/NY sessions.",
    }


@router.post("/auto-execute/disable")
async def disable_auto_execute() -> dict:
    """Disable autonomous demo execution at runtime."""
    settings.auto_execute_demo = False
    from ..services.scheduler import get_scheduler
    sched = get_scheduler()
    if sched.running and sched.get_job("demo_execution"):
        sched.remove_job("demo_execution")
    logger.warning("🛑 Auto-execution DISABLED via API")
    return {"success": True, "auto_execute_demo": False}


@router.get("/data-source")
async def data_source() -> dict:
    """
    Returns the active market data source and connection diagnostics.
    Used by the Operator Dashboard data-source indicator.
    """
    source = settings.market_data_source
    result: dict = {
        "source": source,
        "label":  "CTRADER LIVE" if source == "ctrader" else "INTERNAL / DEMO",
        "live":   source == "ctrader",
    }
    if source == "ctrader":
        from ..integration.ctrader_data import get_feed
        feed = get_feed()
        if feed and feed.is_started:
            result["connected"] = True
            result["stats"]     = feed.stats
        else:
            result["connected"] = False
            result["stats"]     = {}
    else:
        result["connected"] = True   # internal is always "connected"
        result["stats"]     = {}
    return result


@router.get("/status")
async def status() -> dict:
    state, summary, account, exp = await asyncio.gather(
        locks.get_state(),
        get_daily_summary(),
        account_manager.get_account(),
        expansion_manager.get_state(),
    )
    return {
        "mode": settings.mode.value,
        "risk_mode": "expansion" if exp.active else "defensive",
        "is_locked": state.is_locked,
        "kill_switch": state.kill_switch,
        "lock_reason": state.lock_reason.value if state.lock_reason else None,
        "trades_today": summary["total"],
        "wins_today": summary["wins"],
        "losses_today": summary["losses"],
        "pnl_today": round(summary["total_pnl"] or 0.0, 2),
        "drawdown_pct": state.drawdown_pct,
        "daily_loss_limit": risk_manager.compute_daily_loss_limit(account.equity),
        "account_balance": account.balance,
        "equity": account.equity,
        "max_trades_per_day": settings.max_trades_per_day,
        "expansion_trades_remaining": (
            settings.expansion_max_trades - exp.trades_in_window
        ) if exp.active else None,
    }


@router.get("/account")
async def account_state() -> dict:
    account, exp = await asyncio.gather(
        account_manager.get_account(),
        expansion_manager.get_state(),
    )
    sizing = risk_manager.compute_position_pnl(
        equity=account.equity,
        consecutive_losses=account.consecutive_losses,
        expansion_active=exp.active,
    )
    return {
        "starting_balance": account.starting_balance,
        "balance": account.balance,
        "equity": account.equity,
        "peak_equity": account.peak_equity,
        "total_pnl": account.total_pnl,
        "drawdown_pct": account.drawdown_pct,
        "consecutive_losses": account.consecutive_losses,
        "equity_at_day_start": account.equity_at_day_start,
        "last_updated": account.last_updated.isoformat() if account.last_updated else None,
        # Risk limits
        "daily_loss_limit": risk_manager.compute_daily_loss_limit(account.equity),
        "daily_loss_limit_desc": risk_manager.daily_loss_limit_str(account.equity),
        "intraday_dd_limit": risk_manager.compute_intraday_dd_limit(account.equity_at_day_start),
        "intraday_dd_limit_desc": risk_manager.intraday_dd_limit_str(account.equity_at_day_start),
        "total_drawdown_limit": risk_manager.compute_max_drawdown_limit(account.peak_equity),
        "total_drawdown_limit_desc": risk_manager.total_drawdown_limit_str(account.peak_equity),
        # Mode C
        "risk_mode": "expansion" if exp.active else "defensive",
        "effective_risk_pct": sizing["risk_pct"],
        "next_trade_win": sizing["win"],
        "next_trade_loss": sizing["loss"],
        "expansion_active": exp.active,
        "expansion_trades_in_window": exp.trades_in_window,
        "expansion_trades_remaining": (
            settings.expansion_max_trades - exp.trades_in_window
        ) if exp.active else None,
        "expansion_start_equity": exp.start_equity if exp.active else None,
        "expansion_exit_reason": exp.exit_reason,
        # Config
        "risk_per_trade_pct": settings.defensive_risk_pct,
        "expansion_risk_pct": settings.expansion_risk_pct,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "max_total_drawdown_pct": settings.max_total_drawdown_pct,
        "intraday_dd_stop_pct": settings.intraday_dd_stop_pct,
        "consecutive_loss_threshold": settings.consecutive_loss_threshold,
        "consecutive_loss_scale_factor": settings.consecutive_loss_scale_factor,
    }


@router.get("/signals/recent")
async def recent_signals(limit: int = 10) -> list[dict]:
    signals = await storage.get_recent_signals(limit=min(limit, 50))
    return [
        {
            "id": s.id,
            "ts": s.ts.isoformat(),
            "symbol": s.symbol.value,
            "side": s.side.value,
            "entry": s.entry,
            "sl": s.sl,
            "tp": s.tp,
            "score": s.score,
            "status": s.status.value,
            "bias": s.bias.value,
        }
        for s in signals
    ]


@router.get("/report/today")
async def today_report() -> dict:
    return await storage.get_daily_summary()


@router.get("/mode")
async def get_mode() -> dict:
    return {"mode": settings.mode.value}


@router.post("/mode/{mode_name}")
async def set_mode(mode_name: str) -> dict:
    from ..domain.enums import Mode
    try:
        settings.mode = Mode(mode_name.lower())
        return {"mode": settings.mode.value, "success": True}
    except ValueError:
        return {"error": f"Invalid mode: {mode_name}", "success": False}


# ---------------------------------------------------------------------------
# cTrader OAuth endpoints
# ---------------------------------------------------------------------------

@router.get("/auth/ctrader/login")
async def ctrader_login() -> RedirectResponse:
    """
    Initiate cTrader OAuth flow.
    Redirects user to cTrader authorization page.
    """
    auth_url = oauth_service.get_authorization_url()
    return RedirectResponse(url=auth_url)


@router.get("/auth/ctrader/callback")
async def ctrader_callback(code: str | None = None, error: str | None = None) -> dict:
    """
    OAuth callback endpoint.
    Exchanges authorization code for access/refresh tokens.
    
    IMPORTANT: Authorization codes are single-use only.
    Do NOT refresh this page or open the callback URL multiple times.
    """
    if error:
        logger.error("OAuth callback received error: {}", error)
        raise HTTPException(status_code=400, detail=f"OAuth authorization error: {error}")

    if not code:
        logger.error("OAuth callback missing authorization code")
        raise HTTPException(status_code=400, detail="Missing authorization code")

    logger.info("🔑 OAuth callback received | code_length={}", len(code))
    
    try:
        # Exchange authorization code for tokens (single-use only!)
        token_data = await oauth_service.exchange_code_for_token(code)
        
        logger.info("✅ OAuth tokens stored successfully")

        return {
            "success": True,
            "message": "Successfully authenticated with cTrader",
            "environment": settings.ctrader_env,
            "next_step": "Check /api/v1/ctrader/status to verify connection",
        }
    except ValueError as e:
        # cTrader API errors - return detailed debug info
        error_msg = str(e)
        logger.error("❌ cTrader OAuth error: {}", error_msg)
        
        # Try to extract debug info from exception context
        debug_info = {}
        if hasattr(e, '__notes__'):
            for note in e.__notes__:
                if isinstance(note, dict):
                    debug_info = note
                    break
        
        return {
            "success": False,
            "error": "ctrader_oauth_error",
            "message": error_msg,
            "debug": debug_info or {
                "hint": "Check server logs for token exchange details"
            }
        }
    except Exception as e:
        # Unexpected errors
        logger.exception("💥 Unexpected OAuth error: {}", str(e))
        return {
            "success": False,
            "error": "unexpected_error",
            "message": str(e),
            "type": type(e).__name__
        }


@router.get("/ctrader/status")
async def ctrader_status() -> dict:
    """
    Test cTrader connection and return status.
    
    Returns:
        - connected: bool
        - token_valid: bool
        - environment: "demo" or "live"
        - account_id: selected account ID
        - accounts: list of available accounts
        - last_heartbeat: timestamp
    """
    return await oauth_service.test_connection()


@router.get("/ctrader/debug-token")
async def ctrader_debug_token() -> dict:
    """
    Safe token debug endpoint - shows token status without exposing values.
    """
    try:
        access_token = await oauth_service._get_secret("ctrader_access_token")
        refresh_token = await oauth_service._get_secret("ctrader_refresh_token")
        expires_at_str = await oauth_service._get_secret("ctrader_token_expires_at")
        
        seconds_remaining = None
        if expires_at_str:
            try:
                from datetime import datetime
                expires_at = datetime.fromisoformat(expires_at_str)
                seconds_remaining = int((expires_at - datetime.utcnow()).total_seconds())
            except:
                pass
        
        return {
            "token_present": bool(access_token),
            "refresh_token_present": bool(refresh_token),
            "expires_at": expires_at_str or "",
            "seconds_remaining": seconds_remaining,
            "expired": seconds_remaining < 0 if seconds_remaining is not None else None,
        }
    except Exception as e:
        return {
            "error": str(e),
            "token_present": False,
        }


@router.post("/ctrader/refresh-token")
async def ctrader_refresh_token() -> dict:
    """Manually trigger token refresh."""
    try:
        await oauth_service.refresh_access_token()
        return {"success": True, "message": "Token refreshed successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Token refresh failed: {str(e)}")


@router.get("/ctrader/accounts")
async def ctrader_accounts() -> dict:
    """
    Fetch available cTrader accounts.
    
    This is separate from OAuth callback to avoid breaking token exchange.
    If this errors, it indicates the accounts API endpoint needs configuration.
    """
    try:
        logger.info("🔍 Fetching cTrader accounts via API...")
        accounts = await oauth_service.discover_accounts()
        return {
            "success": True,
            "environment": settings.ctrader_env,
            "accounts": accounts,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch accounts: {str(e)}")


@router.get("/ctrader/oauth-url")
async def ctrader_oauth_url() -> dict:
    """
    Debug endpoint: returns the OAuth authorization URL components.
    Use this to verify the correct authorization endpoint is being used.
    """
    from urllib.parse import parse_qs, urlparse
    
    # Generate the full OAuth URL
    auth_url = oauth_service.get_authorization_url()
    
    # Parse it to extract components
    parsed = urlparse(auth_url)
    params = parse_qs(parsed.query)
    
    return {
        "auth_url": auth_url,
        "base_url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
        "client_id": params.get("client_id", [""])[0],
        "redirect_uri": params.get("redirect_uri", [""])[0],
        "scope": params.get("scope", [""])[0],
        "response_type": params.get("response_type", [""])[0],
        "state": params.get("state", [""])[0][:16] + "..." if params.get("state") else None,
        "environment": settings.ctrader_env,
    }


# ---------------------------------------------------------------------------
# Demo Execution Status Endpoints
# ---------------------------------------------------------------------------

@router.get("/execution/status")
async def execution_status() -> dict:
    """
    Get execution service status and open positions.
    
    Returns:
        - mode: Current trading mode
        - open_positions: List of open trades
        - position_count: Number of open positions
        - risk_state: Current risk state
        - ctrader_connected: cTrader connection status (if applicable)
    """
    from ..config import settings
    from ..domain.enums import Mode
    from ..services.ctrader_oauth import oauth_service
    from ..services import locks
    
    # Get open positions
    open_trades = await storage.get_open_trades()
    
    # Get risk state
    risk_state = await locks.get_state()
    
    # Build response
    response = {
        "mode": settings.mode.value,
        "position_count": len(open_trades),
        "open_positions": [
            {
                "id": trade.id,
                "symbol": trade.symbol.value,
                "side": trade.side.value,
                "entry": trade.entry,
                "sl": trade.sl,
                "tp": trade.tp,
                "size": trade.size,
                "pnl": trade.pnl,
                "outcome": trade.outcome.value,
                "ts_open": trade.ts_open.isoformat() if trade.ts_open else None,
            }
            for trade in open_trades
        ],
        "risk_state": {
            "trades_count": risk_state.trades_count,
            "losses_count": risk_state.losses_count,
            "pnl": risk_state.pnl,
            "drawdown_pct": risk_state.drawdown_pct,
            "is_locked": risk_state.is_locked,
            "lock_reason": risk_state.lock_reason.value if risk_state.lock_reason else None,
        },
        "ctrader_connected": False,
    }
    
    # Add cTrader status if in demo/live mode
    if settings.mode in [Mode.DEMO, Mode.LIVE]:
        try:
            ctrader_status = await oauth_service.test_connection()
            response["ctrader_connected"] = ctrader_status.get("connected", False)
        except Exception:
            pass
    
    return response


@router.get("/ctrader/symbols")
async def ctrader_symbols(filter: str = Query("", description="Comma-separated name fragments to filter, e.g. XAU,XAG")) -> list[dict]:
    """
    List tradeable symbols for the configured cTrader account.
    Use ?filter=XAU,XAG to narrow results to gold/silver.
    """
    from ..domain.enums import Mode
    from ..execution.ctrader_execution import ctrader_executor
    from ..integration.ctrader_trading import get_trading_connection
    from ..services.ctrader_oauth import oauth_service

    if settings.mode not in (Mode.DEMO, Mode.LIVE):
        raise HTTPException(status_code=400, detail="Only available in demo/live mode")

    try:
        token = await oauth_service.get_valid_access_token()
        account_id = settings.ctrader_account_id
        conn = await get_trading_connection()
        await conn.authenticate_account(account_id, token)
        fragments = [f.strip() for f in filter.split(",") if f.strip()] or None
        symbols = await conn.get_symbols_for_account(account_id, filter_names=fragments)
        return symbols
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/risk/reset-today")
async def risk_reset_today() -> dict:
    """
    Admin: reset today's risk lock (loss counter, PnL, locked_until_ts).

    Use this when the daily-loss-limit fired during testing and you want to
    manually place a trade via /signals/{id}/execute.  Does NOT affect the
    kill-switch — set that separately if needed.
    """
    from ..data.storage import load_risk_state, save_risk_state
    from ..domain.models import RiskState
    state = await load_risk_state()
    prev = {"pnl": state.pnl, "losses_count": state.losses_count, "lock_reason": state.lock_reason.value if state.lock_reason else None}
    unlocked = RiskState(
        date=state.date,
        trades_count=0,
        losses_count=0,
        pnl=0.0,
        drawdown_pct=0.0,
        locked_until_ts=None,
        lock_reason=None,
        paused_until_ts=None,
        kill_switch=False,
    )
    await save_risk_state(unlocked)
    logger.warning("⚠️  risk/reset-today called — previous state: {}", prev)
    return {"success": True, "previous": prev, "message": "Today's risk lock cleared. You can now execute signals."}


@router.post("/execution/sync-positions")
async def sync_positions() -> dict:
    """
    Manually trigger position sync with cTrader.

    Only works in demo/live mode.
    Set DEBUG=1 env var to always receive error_details in the response.
    """
    import os
    from ..config import settings
    from ..domain.enums import Mode
    from ..execution.ctrader_execution import ctrader_executor

    debug_mode = os.getenv("DEBUG", "0") == "1"

    if settings.mode not in [Mode.DEMO, Mode.LIVE]:
        return {
            "success": False,
            "error": f"Position sync only available in demo/live mode (current: {settings.mode.value})",
        }

    try:
        result = await ctrader_executor.sync_positions()
        response: dict = {
            "success": True,
            "synced": result["synced"],
            "closed": result["closed"],
            "errors": result["errors"],
        }
        # Always include error_details when DEBUG=1 or when there are real errors
        if debug_mode or result.get("error_details"):
            response["error_details"] = result.get("error_details", [])
        return response
    except Exception as e:
        import traceback
        logger.error("sync-positions route unhandled exception:\n{}", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Position sync failed: {str(e)}")


# ---------------------------------------------------------------------------
# Broker-truth reconciliation endpoints
# ---------------------------------------------------------------------------

@router.get("/execution/broker-positions")
async def broker_positions() -> dict:
    """
    Pull the live open-position list directly from the cTrader broker.

    Returns the raw reconcile snapshot so operators can verify what the broker
    believes is open, independent of our local DB state.
    Only available in demo/live mode.
    """
    from ..config import settings
    from ..domain.enums import Mode
    from ..execution.ctrader_execution import ctrader_executor
    from ..integration.ctrader_trading import get_trading_connection
    from ..services.ctrader_oauth import oauth_service
    import ctrader_open_api.messages.OpenApiMessages_pb2 as _m

    if settings.mode not in (Mode.DEMO, Mode.LIVE):
        raise HTTPException(status_code=400, detail="Only available in demo/live mode")

    try:
        token = await oauth_service.get_valid_access_token()
        account_id = settings.ctrader_account_id
        conn = await get_trading_connection()
        await conn.authenticate_account(account_id, token)

        req = _m.ProtoOAReconcileReq()
        req.ctidTraderAccountId = int(account_id)
        rt, rp = await conn.send_and_wait(req.payloadType, req.SerializeToString(), timeout=10.0)

        if rt != 2125:
            err_res = _m.ProtoOAErrorRes()
            try:
                err_res.ParseFromString(rp)
                detail = f"{err_res.errorCode}: {err_res.description}"
            except Exception:
                detail = f"unexpected payloadType={rt}"
            raise HTTPException(status_code=502, detail=f"Broker error — {detail}")

        res = _m.ProtoOAReconcileRes()
        res.ParseFromString(rp)

        positions = []
        for p in res.position:
            positions.append({
                "position_id": str(p.positionId),
                "symbol_id": p.tradeData.symbolId,
                "side": "buy" if p.tradeData.tradeSide == 1 else "sell",
                "volume": p.tradeData.volume / 100,        # cents → lots*100, /100 → lots
                "entry_price": p.price / 100000.0 if p.price else None,
                "open_timestamp_ms": p.tradeData.openTimestamp,
                "unrealised_swap": p.swap / 100.0,
                "commission": p.commission / 100.0,
            })

        return {
            "account_id": account_id,
            "broker_position_count": len(positions),
            "positions": positions,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/execution/db-positions")
async def db_positions() -> dict:
    """
    Return all open positions from the local database.

    These are trades with outcome='open' regardless of mode.
    """
    open_trades = await storage.get_open_trades()
    return {
        "db_position_count": len(open_trades),
        "positions": [
            {
                "id": t.id,
                "symbol": t.symbol.value,
                "side": t.side.value,
                "mode": t.mode.value,
                "entry": t.entry,
                "sl": t.sl,
                "tp": t.tp,
                "size": t.size,
                "pnl": t.pnl,
                "broker_position_id": t.broker_position_id,
                "ts_open": t.ts_open.isoformat() if t.ts_open else None,
            }
            for t in open_trades
        ],
    }


@router.get("/execution/reconcile-report")
async def reconcile_report() -> dict:
    """
    Diff broker open positions against local DB open positions.

    Returns three buckets:
    - matched:   broker position ID exists in both broker and DB
    - broker_only: broker has a position our DB doesn't know about (ghost exposure)
    - db_only:   DB has an open trade with no matching broker position (orphan)

    Only available in demo/live mode.
    """
    from ..config import settings
    from ..domain.enums import Mode
    from ..integration.ctrader_trading import get_trading_connection
    from ..services.ctrader_oauth import oauth_service
    import ctrader_open_api.messages.OpenApiMessages_pb2 as _m

    if settings.mode not in (Mode.DEMO, Mode.LIVE):
        raise HTTPException(status_code=400, detail="Only available in demo/live mode")

    try:
        # --- Broker snapshot ---
        token = await oauth_service.get_valid_access_token()
        account_id = settings.ctrader_account_id
        conn = await get_trading_connection()
        await conn.authenticate_account(account_id, token)

        req = _m.ProtoOAReconcileReq()
        req.ctidTraderAccountId = int(account_id)
        rt, rp = await conn.send_and_wait(req.payloadType, req.SerializeToString(), timeout=10.0)

        if rt != 2125:
            err_res = _m.ProtoOAErrorRes()
            try:
                err_res.ParseFromString(rp)
                detail = f"{err_res.errorCode}: {err_res.description}"
            except Exception:
                detail = f"unexpected payloadType={rt}"
            raise HTTPException(status_code=502, detail=f"Broker error — {detail}")

        res = _m.ProtoOAReconcileRes()
        res.ParseFromString(rp)
        broker_ids: dict[str, dict] = {
            str(p.positionId): {
                "symbol_id": p.tradeData.symbolId,
                "side": "buy" if p.tradeData.tradeSide == 1 else "sell",
                "volume_lots": p.tradeData.volume / 10_000_000,  # cTrader: 1 lot = 10_000_000 units
                "entry_price": p.price / 100000.0 if p.price else None,
            }
            for p in res.position
        }

        # --- DB snapshot (demo/live only) ---
        open_trades = await storage.get_open_trades()
        live_trades = [t for t in open_trades if t.mode in (Mode.DEMO, Mode.LIVE)]

        db_by_remote: dict[str, dict] = {}
        db_no_remote: list[dict] = []
        for t in live_trades:
            if t.broker_position_id:
                db_by_remote[str(t.broker_position_id)] = {
                    "db_trade_id": t.id,
                    "symbol": t.symbol.value,
                    "side": t.side.value,
                    "size": t.size,
                    "entry": t.entry,
                }
            else:
                db_no_remote.append({
                    "db_trade_id": t.id,
                    "symbol": t.symbol.value,
                    "side": t.side.value,
                    "entry": t.entry,
                })

        # --- Diff ---
        matched = []
        broker_only = []
        db_only = list(db_no_remote)  # DB rows with no remote ID are definitionally orphans

        all_ids = set(broker_ids) | set(db_by_remote)
        for pid in all_ids:
            in_broker = pid in broker_ids
            in_db     = pid in db_by_remote
            if in_broker and in_db:
                matched.append({"position_id": pid, "broker": broker_ids[pid], "db": db_by_remote[pid]})
            elif in_broker:
                broker_only.append({"position_id": pid, **broker_ids[pid]})
            else:
                db_only.append({"position_id": pid, **db_by_remote[pid]})

        healthy = (len(broker_only) == 0 and len(db_only) == 0)
        return {
            "healthy": healthy,
            "matched_count": len(matched),
            "broker_only_count": len(broker_only),   # ghost exposure — dangerous
            "db_only_count": len(db_only),            # orphaned local trades
            "matched": matched,
            "broker_only": broker_only,
            "db_only": db_only,
            "note": (
                "healthy=true means broker and DB are in full agreement. "
                "broker_only entries represent ghost exposure (broker has open risk we don't track). "
                "db_only entries are orphaned local records (no matching broker position)."
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Control plane endpoints
# ---------------------------------------------------------------------------

@router.post("/control/kill-switch")
async def kill_switch(request: Request) -> dict:
    """
    Hard-stop all trading immediately.

    POST body: {"enable": true}   — activate kill switch (blocks all new orders)
    POST body: {"enable": false}  — deactivate kill switch

    Kill switch state persists across restarts (stored in DB).
    It does NOT cancel existing open broker positions — use broker panel for that.
    """
    from ..data.storage import load_risk_state, save_risk_state
    from ..data.storage import log_execution_event

    body = await request.json()
    enable: bool = bool(body.get("enable", True))

    state = await load_risk_state()
    prev = state.kill_switch
    state.kill_switch = enable
    await save_risk_state(state)

    action = "ACTIVATED" if enable else "DEACTIVATED"
    logger.warning("\u26a0\ufe0f  kill-switch {}", action)
    await log_execution_event(
        "kill_switch",
        detail=f"kill_switch {'enabled' if enable else 'disabled'}; prev={prev}",
    )
    return {
        "success": True,
        "kill_switch": enable,
        "message": f"Kill switch {action}. All new orders are {'blocked' if enable else 'permitted'}.",
    }


@router.post("/control/pause")
async def pause_trading(request: Request) -> dict:
    """
    Pause all new order placement for a specified number of minutes.

    POST body: {"minutes": 30}

    Use this for news event blackouts or manual intervention windows.
    Does NOT affect kill switch or daily loss counters.
    """
    from datetime import datetime, timedelta
    from ..data.storage import load_risk_state, save_risk_state
    from ..data.storage import log_execution_event
    from ..domain.enums import LockReason

    body = await request.json()
    minutes: int = int(body.get("minutes", 30))
    if minutes < 1 or minutes > 1440:
        raise HTTPException(status_code=422, detail="minutes must be between 1 and 1440")

    state = await load_risk_state()
    resume_at = datetime.utcnow() + timedelta(minutes=minutes)
    state.paused_until_ts = resume_at
    state.lock_reason = LockReason.PAUSED
    await save_risk_state(state)

    logger.warning("\u23f8\ufe0f  trading paused for {} min (until {})", minutes, resume_at.isoformat())
    await log_execution_event(
        "pause",
        detail=f"paused for {minutes}m, resumes at {resume_at.isoformat()}",
    )
    return {
        "success": True,
        "paused_until": resume_at.isoformat(),
        "minutes": minutes,
        "message": f"Trading paused for {minutes} minutes. Resumes at {resume_at.isoformat()} UTC.",
    }


@router.post("/control/resume")
async def resume_trading() -> dict:
    """
    Resume trading by clearing manual pause and optionally the kill switch.

    This ONLY clears:
    - paused_until_ts (manual pause)
    - kill_switch

    It does NOT reset daily loss counters or cooldown locks — use /risk/reset-today for that.
    """
    from ..data.storage import load_risk_state, save_risk_state
    from ..data.storage import log_execution_event

    state = await load_risk_state()
    prev = {
        "kill_switch": state.kill_switch,
        "paused_until_ts": state.paused_until_ts.isoformat() if state.paused_until_ts else None,
    }
    state.kill_switch = False
    state.paused_until_ts = None
    # Only clear lock_reason if it was PAUSED
    from ..domain.enums import LockReason
    if state.lock_reason == LockReason.PAUSED:
        state.lock_reason = None
    await save_risk_state(state)

    logger.info("\u25b6\ufe0f  trading resumed (kill_switch cleared, pause cleared)")
    await log_execution_event("resume", detail=f"resumed; prev={prev}")
    return {
        "success": True,
        "message": "Trading resumed. Kill switch cleared, manual pause cleared.",
        "previous": prev,
    }


@router.get("/risk/status")
async def risk_status() -> dict:
    """
    Detailed risk status with human-readable time-remaining for each lock.

    Returns a single comprehensive view of all risk gates:
    - kill_switch
    - paused_until_ts
    - locked_until_ts (cooldown)
    - max_trades_per_day progress
    - max_losses_per_day progress
    - daily_dd_cap progress
    - expansion mode status
    """
    from datetime import datetime
    from ..config import settings
    from ..services import locks
    from ..data.storage import load_account_state, load_expansion_state

    state = await locks.get_state()
    account = await load_account_state()
    expansion = await load_expansion_state()

    now = datetime.utcnow()

    def _remaining_min(ts) -> int | None:
        if ts and now < ts:
            return max(0, int((ts - now).total_seconds() // 60))
        return None

    # Which gates are currently blocking?
    blocking: list[str] = []
    if state.kill_switch:
        blocking.append("kill_switch")
    if state.paused_until_ts and now < state.paused_until_ts:
        blocking.append("paused")
    if state.locked_until_ts and now < state.locked_until_ts:
        reason = state.lock_reason.value if state.lock_reason else "cooldown"
        blocking.append(reason)
    if state.trades_count >= settings.max_trades_per_day:
        blocking.append("max_trades")
    if state.losses_count >= settings.max_losses_per_day:
        blocking.append("max_losses")
    if state.drawdown_pct >= settings.daily_dd_cap_pct:
        blocking.append("daily_dd_cap")

    return {
        "can_trade": len(blocking) == 0,
        "blocking_gates": blocking,
        "date": state.date,
        "kill_switch": state.kill_switch,
        "pause": {
            "active": bool(state.paused_until_ts and now < state.paused_until_ts),
            "resumes_at": state.paused_until_ts.isoformat() if state.paused_until_ts else None,
            "remaining_min": _remaining_min(state.paused_until_ts),
        },
        "cooldown": {
            "active": bool(state.locked_until_ts and now < state.locked_until_ts),
            "expires_at": state.locked_until_ts.isoformat() if state.locked_until_ts else None,
            "remaining_min": _remaining_min(state.locked_until_ts),
            "reason": state.lock_reason.value if state.lock_reason else None,
        },
        "daily_counters": {
            "trades": {"used": state.trades_count, "max": settings.max_trades_per_day},
            "losses": {"used": state.losses_count, "max": settings.max_losses_per_day},
            "pnl": round(state.pnl, 2),
            "drawdown_pct": round(state.drawdown_pct, 3),
            "dd_cap_pct": settings.daily_dd_cap_pct,
        },
        "account": {
            "equity": round(account.equity, 2),
            "balance": round(account.balance, 2),
            "peak_equity": round(account.peak_equity, 2),
            "equity_at_day_start": round(account.equity_at_day_start, 2),
        },
        "expansion": {
            "active": expansion.active,
            "trades_in_window": expansion.trades_in_window,
            "consecutive_losses": expansion.consecutive_losses,
        },
    }


@router.get("/execution/events")
async def execution_events(
    limit: int = 50,
    event_type: str | None = None,
) -> dict:
    """
    Return the most recent execution events for operational monitoring.

    Covers: order_placed, order_rejected, position_closed, sync_error,
            kill_switch, pause, resume, mode_change

    Optional ?event_type=sync_error to filter by type.
    """
    from ..data.storage import get_execution_events

    limit = min(limit, 200)
    events = await get_execution_events(limit=limit, event_type=event_type)
    return {
        "count": len(events),
        "limit": limit,
        "filter": event_type,
        "events": events,
    }


# ---------------------------------------------------------------------------
# Trade Journal endpoints
# ---------------------------------------------------------------------------

@router.get("/trades/{trade_id}")
async def get_trade_journal(trade_id: int) -> dict:
    """
    Return full journal record for a single trade, including broker metadata,
    execution latency, entry/exit slippage, and spread captured at order time.
    """
    from ..data.storage import get_trade_by_id

    trade = await get_trade_by_id(trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    return {
        "id": trade.id,
        "signal_id": trade.signal_id,
        "symbol": trade.symbol.value,
        "side": trade.side.value,
        "mode": trade.mode.value,
        "outcome": trade.outcome.value,
        "ts_open": trade.ts_open.isoformat() if trade.ts_open else None,
        "ts_close": trade.ts_close.isoformat() if trade.ts_close else None,
        # Execution
        "entry": trade.entry,
        "exit_price": trade.exit_price,
        "sl": trade.sl,
        "tp": trade.tp,
        "size": trade.size,
        "pnl": trade.pnl,
        "mae": trade.max_adverse_excursion,
        # Broker journal
        "broker": {
            "position_id": trade.broker_position_id,
            "order_id": trade.broker_order_id,
            "execution_latency_ms": trade.execution_latency_ms,
            "entry_slippage": trade.entry_slippage,      # + = worse fill
            "exit_slippage": trade.exit_slippage,         # + = worse close
            "spread_at_entry": trade.spread_at_entry,
        },
    }


@router.get("/trades")
async def list_trades(
    mode: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
) -> dict:
    """
    List recent trades.  Optional filters: mode (demo/paper/live), outcome (open/closed/win/loss/void).
    Returns trades newest-first, capped at limit (max 200).
    """
    import aiosqlite
    from ..domain.models import TradeResult
    from ..data.storage import DB_PATH, _row_to_trade

    limit = min(limit, 200)
    conditions = []
    params: list = []

    if mode:
        conditions.append("mode = ?")
        params.append(mode)
    if outcome:
        conditions.append("outcome = ?")
        params.append(outcome)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT * FROM trades {where} ORDER BY id DESC LIMIT ?",
            params,
        )
        rows = await cur.fetchall()

    trades = [_row_to_trade(r) for r in rows]
    return {
        "count": len(trades),
        "trades": [
            {
                "id": t.id,
                "signal_id": t.signal_id,
                "symbol": t.symbol.value,
                "side": t.side.value,
                "mode": t.mode.value,
                "outcome": t.outcome.value,
                "entry": t.entry,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "size": t.size,
                "ts_open": t.ts_open.isoformat() if t.ts_open else None,
                "ts_close": t.ts_close.isoformat() if t.ts_close else None,
                "broker_position_id": t.broker_position_id,
                "execution_latency_ms": t.execution_latency_ms,
                "entry_slippage": t.entry_slippage,
                "spread_at_entry": t.spread_at_entry,
            }
            for t in trades
        ],
    }


# ---------------------------------------------------------------------------
# Analytics: Performance
# ---------------------------------------------------------------------------

@router.get("/analytics/performance")
async def analytics_performance(
    mode: str = "demo",
    days: int = 30,
) -> dict:
    """
    Strategy performance metrics computed from real journal data.

    Metrics:
    - win_rate, total_trades, wins, losses
    - expectancy (avg PnL per trade)
    - avg_rr (average realised R:R)
    - profit_factor (gross profit / gross loss)
    - max_drawdown_usd (peak-to-trough cumulative PnL)
    - results_by_hour (UTC hour → {trades, wins, avg_pnl})
    - results_by_symbol
    - results_by_session (London / NewYork / Other)
    """
    import aiosqlite
    from datetime import datetime, timedelta, timezone
    from ..data.storage import DB_PATH

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Closed trades only (win/loss/closed)
        cur = await db.execute(
            """
            SELECT id, symbol, side, entry, sl, tp, size, outcome, pnl,
                   exit_price, ts_open, ts_close, entry_slippage
            FROM trades
            WHERE mode = ?
              AND outcome IN ('win','loss','closed')
              AND ts_open >= ?
            ORDER BY ts_open
            """,
            (mode, since),
        )
        rows = await cur.fetchall()

    if not rows:
        return {"error": f"No closed {mode} trades in the last {days} days", "trades": 0}

    # ── Core stats ─────────────────────────────────────────────────────────
    total = len(rows)
    wins   = sum(1 for r in rows if r["outcome"] == "win" or r["pnl"] > 0)
    losses = sum(1 for r in rows if r["outcome"] == "loss" or r["pnl"] < 0)
    total_pnl = sum(r["pnl"] for r in rows)
    gross_profit = sum(r["pnl"] for r in rows if r["pnl"] > 0)
    gross_loss   = abs(sum(r["pnl"] for r in rows if r["pnl"] < 0))
    win_rate     = round(wins / total, 4) if total else 0.0
    expectancy   = round(total_pnl / total, 4) if total else 0.0
    profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else None

    # ── R:R — only computable when we have entry + sl + pnl ────────────────
    rr_values = []
    for r in rows:
        sl_dist = abs(r["entry"] - r["sl"]) if r["entry"] and r["sl"] else 0
        if sl_dist > 0 and r["size"] and r["pnl"] != 0:
            risk_usd = sl_dist * r["size"]
            rr_values.append(r["pnl"] / risk_usd)
    avg_rr = round(sum(rr_values) / len(rr_values), 3) if rr_values else None

    # ── Max drawdown (running peak-to-trough on cumulative PnL) ────────────
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for r in rows:
        cum += r["pnl"]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    # ── By UTC hour ────────────────────────────────────────────────────────
    hour_stats: dict[int, dict] = {}
    for r in rows:
        try:
            h = datetime.fromisoformat(r["ts_open"]).hour
        except Exception:
            continue
        hs = hour_stats.setdefault(h, {"trades": 0, "wins": 0, "pnl": 0.0})
        hs["trades"] += 1
        hs["pnl"] = round(hs["pnl"] + r["pnl"], 4)
        if r["pnl"] > 0:
            hs["wins"] += 1
    for hs in hour_stats.values():
        hs["win_rate"] = round(hs["wins"] / hs["trades"], 3) if hs["trades"] else 0.0
        hs["avg_pnl"]  = round(hs["pnl"] / hs["trades"], 4) if hs["trades"] else 0.0

    # ── By symbol ──────────────────────────────────────────────────────────
    sym_stats: dict[str, dict] = {}
    for r in rows:
        sym = r["symbol"]
        ss = sym_stats.setdefault(sym, {"trades": 0, "wins": 0, "pnl": 0.0})
        ss["trades"] += 1
        ss["pnl"] = round(ss["pnl"] + r["pnl"], 4)
        if r["pnl"] > 0:
            ss["wins"] += 1
    for ss in sym_stats.values():
        ss["win_rate"] = round(ss["wins"] / ss["trades"], 3) if ss["trades"] else 0.0

    # ── By session (London 07–16, NY 13–21, Overlap 13–16) ─────────────────
    sess_stats: dict[str, dict] = {}
    def _session(h: int) -> str:
        if 13 <= h < 16:  return "overlap"
        if 7  <= h < 16:  return "london"
        if 13 <= h < 21:  return "new_york"
        return "off_hours"

    for r in rows:
        try:
            h = datetime.fromisoformat(r["ts_open"]).hour
        except Exception:
            continue
        sn = _session(h)
        ss2 = sess_stats.setdefault(sn, {"trades": 0, "wins": 0, "pnl": 0.0})
        ss2["trades"] += 1
        ss2["pnl"] = round(ss2["pnl"] + r["pnl"], 4)
        if r["pnl"] > 0:
            ss2["wins"] += 1
    for ss2 in sess_stats.values():
        ss2["win_rate"] = round(ss2["wins"] / ss2["trades"], 3) if ss2["trades"] else 0.0

    return {
        "mode": mode,
        "days_back": days,
        "trades":         total,
        "wins":           wins,
        "losses":         losses,
        "win_rate":       win_rate,
        "expectancy_usd": expectancy,
        "avg_rr":         avg_rr,
        "profit_factor":  profit_factor,
        "total_pnl_usd":  round(total_pnl, 2),
        "gross_profit":   round(gross_profit, 2),
        "gross_loss":     round(gross_loss, 2),
        "max_drawdown_usd": round(max_dd, 2),
        "by_hour":    {str(h): v for h, v in sorted(hour_stats.items())},
        "by_symbol":  sym_stats,
        "by_session": sess_stats,
    }


# ---------------------------------------------------------------------------
# Analytics: Execution Quality
# ---------------------------------------------------------------------------

@router.get("/analytics/execution-quality")
async def analytics_execution_quality(
    mode: str = "demo",
    days: int = 30,
) -> dict:
    """
    Broker execution quality metrics.

    Reports:
    - avg/median entry slippage (+ = you paid more, - = price improved)
    - avg/median exit slippage
    - avg spread at entry
    - avg fill latency (ms)
    - worst/best fill
    - slippage distribution by symbol and UTC hour
    """
    import aiosqlite
    import statistics
    from datetime import datetime, timedelta, timezone
    from ..data.storage import DB_PATH

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, symbol, outcome, pnl, ts_open,
                   entry_slippage, exit_slippage,
                   spread_at_entry, execution_latency_ms
            FROM trades
            WHERE mode = ?
              AND ts_open >= ?
              AND entry_slippage IS NOT NULL
            ORDER BY ts_open
            """,
            (mode, since),
        )
        rows = await cur.fetchall()

    if not rows:
        return {
            "error": f"No {mode} trades with journal data in the last {days} days — "
                     "journal fields populate on new executions only",
            "trades_with_journal": 0,
        }

    def _safe_stats(vals: list[float]) -> dict:
        if not vals:
            return {"count": 0, "avg": None, "median": None, "min": None, "max": None}
        return {
            "count":  len(vals),
            "avg":    round(statistics.mean(vals), 5),
            "median": round(statistics.median(vals), 5),
            "min":    round(min(vals), 5),
            "max":    round(max(vals), 5),
        }

    entry_slippage = [r["entry_slippage"] for r in rows if r["entry_slippage"] is not None]
    exit_slippage  = [r["exit_slippage"]  for r in rows if r["exit_slippage"]  is not None]
    spreads        = [r["spread_at_entry"] for r in rows if r["spread_at_entry"] is not None]
    latencies      = [r["execution_latency_ms"] for r in rows if r["execution_latency_ms"] is not None]

    # ── By symbol ──────────────────────────────────────────────────────────
    sym_slip: dict[str, list[float]] = {}
    for r in rows:
        if r["entry_slippage"] is not None:
            sym_slip.setdefault(r["symbol"], []).append(r["entry_slippage"])

    # ── By UTC hour ────────────────────────────────────────────────────────
    hour_slip: dict[int, list[float]] = {}
    for r in rows:
        if r["entry_slippage"] is not None:
            try:
                h = datetime.fromisoformat(r["ts_open"]).hour
                hour_slip.setdefault(h, []).append(r["entry_slippage"])
            except Exception:
                pass

    # ── Slippage cost estimate (assuming spread betting £/$1/point/lot) ────
    total_slippage_cost = sum(
        abs(r["entry_slippage"] or 0) * 1000  # approx: 0.01 lot × 100000 pts/lot × slippage
        for r in rows
    )

    return {
        "mode": mode,
        "days_back": days,
        "trades_with_journal": len(rows),
        "entry_slippage": _safe_stats(entry_slippage),
        "exit_slippage":  _safe_stats(exit_slippage),
        "spread_at_entry": _safe_stats(spreads),
        "fill_latency_ms": _safe_stats(latencies),
        "by_symbol": {
            sym: _safe_stats(vals)
            for sym, vals in sym_slip.items()
        },
        "by_hour": {
            str(h): _safe_stats(vals)
            for h, vals in sorted(hour_slip.items())
        },
        "note": (
            "entry_slippage > 0 means worse fill than signal price. "
            "exit_slippage > 0 means worse close than TP/SL reference. "
            "Journal fields only available on trades executed after the v2 upgrade."
        ),
    }


