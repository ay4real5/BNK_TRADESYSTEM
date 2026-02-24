"""FastAPI route definitions."""

from __future__ import annotations

from fastapi import APIRouter

from ..config import settings
from ..data import storage
from ..services import locks

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "mode": settings.mode.value}


@router.get("/status")
async def status() -> dict:
    state = await locks.get_state()
    return {
        "mode": settings.mode.value,
        "is_locked": state.is_locked,
        "kill_switch": state.kill_switch,
        "trades_today": state.trades_count,
        "losses_today": state.losses_count,
        "pnl_today": state.pnl,
        "drawdown_pct": state.drawdown_pct,
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
