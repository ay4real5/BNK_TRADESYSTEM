"""
Tests for the three trading gates introduced in Phase 4:
  - Session gate     (London 07-16 / NY 13-21 UTC)
  - Volatility gate  (ATR >= min AND spread/ATR <= max_ratio)
  - News blackout    (±buffer_minutes around high-impact events)
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.domain.enums import LockReason, Symbol
from app.domain.errors import LockError
from app.domain.models import AccountState, RiskState


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _basic_account() -> AccountState:
    return AccountState(equity=10_000, peak_equity=10_000, equity_at_day_start=10_000)


def _unlocked_state() -> RiskState:
    """Fresh risk state — no locks, zero counters."""
    return RiskState(date=datetime.utcnow().strftime("%Y-%m-%d"))


def _make_news_lock(hour_offset: int = 0, description: str = "NFP") -> list[dict]:
    """Return a single in-window event centred `hour_offset` hours from now."""
    now = datetime.utcnow()
    start = now - timedelta(minutes=5) + timedelta(hours=hour_offset)
    end   = now + timedelta(minutes=5) + timedelta(hours=hour_offset)
    return [{
        "id": 1,
        "description": description,
        "start": start.isoformat(),
        "end":   end.isoformat(),
        "created_at": now.isoformat(),
    }]


# ═══════════════════════════════════════════════════════════════════════════
#  Session gate — locks.check_can_trade
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_session_gate_blocks_outside_hours():
    """3 AM UTC — outside both London and NY windows — must raise OUT_OF_SESSION."""
    mock_now = MagicMock()
    mock_now.hour = 3  # 03:00 UTC — outside London(7-16) and NY(13-21)

    with (
        patch("app.services.locks.datetime") as mock_dt,
        patch("app.services.locks.account_manager.get_account",
              new=AsyncMock(return_value=_basic_account())),
        patch("app.services.news_filter._load_events", return_value=[]),
        patch("app.services.locks._log_session_block", new=AsyncMock()),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        mock_dt.now.return_value = mock_now
        mock_dt.utcnow.return_value = datetime.utcnow()

        with pytest.raises(LockError) as exc_info:
            from app.services import locks
            await locks.check_can_trade(state=_unlocked_state())

    assert LockReason.OUT_OF_SESSION.value in str(exc_info.value)
    assert "UTC hour=3" in str(exc_info.value)


@pytest.mark.asyncio
async def test_session_gate_allows_london_hours():
    """9 AM UTC — inside London window — must NOT raise for session gate."""
    mock_now = MagicMock()
    mock_now.hour = 9  # 09:00 UTC inside London (07:00–16:00)

    with (
        patch("app.services.locks.datetime") as mock_dt,
        patch("app.services.locks.account_manager.get_account",
              new=AsyncMock(return_value=_basic_account())),
        patch("app.services.news_filter._load_events", return_value=[]),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        mock_dt.now.return_value = mock_now
        mock_dt.utcnow.return_value = datetime.utcnow()

        from app.services import locks
        # Should not raise — return RiskState
        state = await locks.check_can_trade(state=_unlocked_state())
    assert state is not None


@pytest.mark.asyncio
async def test_session_gate_allows_ny_overlap():
    """14:30 UTC — inside both London + NY (overlap) — must pass."""
    mock_now = MagicMock()
    mock_now.hour = 14  # 14:00 UTC inside NY (13:00–21:00)

    with (
        patch("app.services.locks.datetime") as mock_dt,
        patch("app.services.locks.account_manager.get_account",
              new=AsyncMock(return_value=_basic_account())),
        patch("app.services.news_filter._load_events", return_value=[]),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        mock_dt.now.return_value = mock_now
        mock_dt.utcnow.return_value = datetime.utcnow()

        from app.services import locks
        state = await locks.check_can_trade(state=_unlocked_state())
    assert state is not None


@pytest.mark.asyncio
async def test_session_gate_disabled_allows_any_hour():
    """With session_gate_enabled=False, even 3 AM UTC should pass the session check."""
    mock_now = MagicMock()
    mock_now.hour = 3

    with (
        patch.object(settings, "session_gate_enabled", False),
        patch("app.services.locks.datetime") as mock_dt,
        patch("app.services.locks.account_manager.get_account",
              new=AsyncMock(return_value=_basic_account())),
        patch("app.services.news_filter._load_events", return_value=[]),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        mock_dt.now.return_value = mock_now
        mock_dt.utcnow.return_value = datetime.utcnow()

        from app.services import locks
        state = await locks.check_can_trade(state=_unlocked_state())
    assert state is not None


# ═══════════════════════════════════════════════════════════════════════════
#  News gate — locks.check_can_trade + news_filter.is_news_window
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_news_gate_blocks_during_event():
    """When a news event is currently active, check_can_trade must raise NEWS_FILTER."""
    mock_now = MagicMock()
    mock_now.hour = 9  # inside London session so session gate passes

    with (
        patch("app.services.locks.datetime") as mock_dt,
        patch("app.services.locks.account_manager.get_account",
              new=AsyncMock(return_value=_basic_account())),
        # inject a currently-active news event
        patch("app.services.news_filter._load_events",
              return_value=_make_news_lock()),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        mock_dt.now.return_value = mock_now
        mock_dt.utcnow.return_value = datetime.utcnow()

        from app.services import locks
        with pytest.raises(LockError) as exc_info:
            await locks.check_can_trade(state=_unlocked_state())

    assert LockReason.NEWS_FILTER.value in str(exc_info.value)
    assert "NFP" in str(exc_info.value)


@pytest.mark.asyncio
async def test_news_gate_passes_when_no_events():
    """With no news events scheduled, the gate must not block."""
    mock_now = MagicMock()
    mock_now.hour = 9

    with (
        patch("app.services.locks.datetime") as mock_dt,
        patch("app.services.locks.account_manager.get_account",
              new=AsyncMock(return_value=_basic_account())),
        patch("app.services.news_filter._load_events", return_value=[]),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        mock_dt.now.return_value = mock_now
        mock_dt.utcnow.return_value = datetime.utcnow()

        from app.services import locks
        state = await locks.check_can_trade(state=_unlocked_state())
    assert state is not None


@pytest.mark.asyncio
async def test_news_gate_passes_for_future_event_outside_buffer():
    """Event 2 hours in the future (beyond 15-min buffer) must not block."""
    now = datetime.utcnow()
    future_event = [{
        "id": 1,
        "description": "CPI",
        "start": (now + timedelta(hours=2)).isoformat(),
        "end":   (now + timedelta(hours=2, minutes=30)).isoformat(),
        "created_at": now.isoformat(),
    }]

    mock_now = MagicMock()
    mock_now.hour = 9

    with (
        patch("app.services.locks.datetime") as mock_dt,
        patch("app.services.locks.account_manager.get_account",
              new=AsyncMock(return_value=_basic_account())),
        patch("app.services.news_filter._load_events", return_value=future_event),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        mock_dt.now.return_value = mock_now
        mock_dt.utcnow.return_value = datetime.utcnow()

        from app.services import locks
        state = await locks.check_can_trade(state=_unlocked_state())
    assert state is not None


# ═══════════════════════════════════════════════════════════════════════════
#  is_news_window — unit tests for the news_filter function itself
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_is_news_window_blocked():
    from app.services.news_filter import is_news_window
    with (
        patch("app.services.news_filter._load_events",
              return_value=_make_news_lock()),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        blocked, reason = await is_news_window(buffer_minutes=15)
    assert blocked is True
    assert "NFP" in reason


@pytest.mark.asyncio
async def test_is_news_window_not_blocked_empty():
    from app.services.news_filter import is_news_window
    with patch("app.services.news_filter._load_events", return_value=[]):
        blocked, reason = await is_news_window(buffer_minutes=15)
    assert blocked is False
    assert reason == ""


@pytest.mark.asyncio
async def test_is_news_window_not_blocked_expired():
    """Event that ended 2 hours ago must not block."""
    now = datetime.utcnow()
    expired = [{
        "id": 1,
        "description": "Old NFP",
        "start": (now - timedelta(hours=2, minutes=30)).isoformat(),
        "end":   (now - timedelta(hours=2)).isoformat(),
        "created_at": now.isoformat(),
    }]
    from app.services.news_filter import is_news_window
    with patch("app.services.news_filter._load_events", return_value=expired):
        blocked, reason = await is_news_window(buffer_minutes=15)
    assert blocked is False


# ═══════════════════════════════════════════════════════════════════════════
#  news_filter CRUD helpers
# ═══════════════════════════════════════════════════════════════════════════

def test_add_remove_list_events(tmp_path):
    from app.services import news_filter
    events_file = tmp_path / "news_events.json"
    with patch.object(settings, "news_events_file", str(events_file)):
        now = datetime.utcnow()
        evt = news_filter.add_event(
            now + timedelta(hours=1),
            end=now + timedelta(hours=1, minutes=30),
            description="TestNFP",
        )
        assert evt["id"] == 1
        assert evt["description"] == "TestNFP"

        events = news_filter.list_events()
        assert len(events) == 1

        removed = news_filter.remove_event(1)
        assert removed is True

        removed_again = news_filter.remove_event(1)
        assert removed_again is False

        assert news_filter.list_events() == []


def test_clear_expired_events(tmp_path):
    from app.services import news_filter
    events_file = tmp_path / "news_events.json"
    with patch.object(settings, "news_events_file", str(events_file)):
        now = datetime.utcnow()
        # OldEvent ended 1 hour ago — recent enough to not be auto-pruned on insert
        # but clearly past its end time so clear_expired should remove it
        news_filter.add_event(
            now - timedelta(hours=2),
            end=now - timedelta(hours=1),
            description="OldEvent",
        )
        news_filter.add_event(
            now + timedelta(hours=2),
            end=now + timedelta(hours=2, minutes=30),
            description="FutureEvent",
        )
        removed = news_filter.clear_expired()
        assert removed == 1
        remaining = news_filter.list_events()
        assert len(remaining) == 1
        assert remaining[0]["description"] == "FutureEvent"


# ═══════════════════════════════════════════════════════════════════════════
#  Volatility gate — volatility_gate.check_volatility
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_volatility_gate_disabled_passes():
    """When gate is disabled, check_volatility should always return without raising."""
    from app.services.volatility_gate import check_volatility
    with patch.object(settings, "volatility_gate_enabled", False):
        await check_volatility(Symbol.XAUUSD)  # should not raise


@pytest.mark.asyncio
async def test_volatility_gate_atr_too_low_raises():
    """ATR below minimum should raise HIGH_VOLATILITY."""
    from app.services import volatility_gate

    with (
        patch.object(settings, "volatility_gate_enabled", True),
        patch.object(settings, "atr_min_xauusd", 0.30),
        patch(
            "app.services.volatility_gate._compute_atr_m5",
            new=AsyncMock(return_value=0.10),  # well below 0.30
        ),
        patch(
            "app.services.volatility_gate._get_latest_spread",
            new=AsyncMock(return_value=0.02),
        ),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        with pytest.raises(LockError) as exc_info:
            await volatility_gate.check_volatility(Symbol.XAUUSD)

    assert LockReason.HIGH_VOLATILITY.value in str(exc_info.value)
    assert "atr_too_low" in str(exc_info.value).lower() or "ATR" in str(exc_info.value)


@pytest.mark.asyncio
async def test_volatility_gate_spread_too_wide_raises():
    """Spread/ATR ratio over limit should raise HIGH_VOLATILITY."""
    from app.services import volatility_gate

    with (
        patch.object(settings, "volatility_gate_enabled", True),
        patch.object(settings, "atr_min_xauusd", 0.30),
        patch.object(settings, "spread_atr_max_ratio", 0.25),
        patch(
            "app.services.volatility_gate._compute_atr_m5",
            new=AsyncMock(return_value=1.00),  # ATR OK
        ),
        patch(
            "app.services.volatility_gate._get_latest_spread",
            new=AsyncMock(return_value=0.50),  # spread = 50% of ATR → too wide
        ),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        with pytest.raises(LockError) as exc_info:
            await volatility_gate.check_volatility(Symbol.XAUUSD)

    assert LockReason.HIGH_VOLATILITY.value in str(exc_info.value)
    assert "spread" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_volatility_gate_healthy_passes():
    """ATR above min AND spread below ratio limit — must not raise."""
    from app.services import volatility_gate

    with (
        patch.object(settings, "volatility_gate_enabled", True),
        patch.object(settings, "atr_min_xauusd", 0.30),
        patch.object(settings, "spread_atr_max_ratio", 0.25),
        patch(
            "app.services.volatility_gate._compute_atr_m5",
            new=AsyncMock(return_value=1.50),
        ),
        patch(
            "app.services.volatility_gate._get_latest_spread",
            new=AsyncMock(return_value=0.20),  # 20/150 = 13% < 25%
        ),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        await volatility_gate.check_volatility(Symbol.XAUUSD)  # no raise


@pytest.mark.asyncio
async def test_volatility_gate_fail_open_on_no_data():
    """When _compute_atr_m5 raises InsufficientData, gate must fail-open (no raise)."""
    from app.services import volatility_gate

    # Access the module-level private exception class
    InsufficientData = volatility_gate.InsufficientData  # type: ignore[attr-defined]

    with (
        patch.object(settings, "volatility_gate_enabled", True),
        patch(
            "app.services.volatility_gate._compute_atr_m5",
            side_effect=InsufficientData("not enough rows"),
        ),
    ):
        await volatility_gate.check_volatility(Symbol.XAUUSD)  # must not raise


@pytest.mark.asyncio
async def test_volatility_gate_fail_open_on_exception():
    """Unexpected DB errors must not block trading (fail-open)."""
    from app.services import volatility_gate

    with (
        patch.object(settings, "volatility_gate_enabled", True),
        patch(
            "app.services.volatility_gate._compute_atr_m5",
            side_effect=RuntimeError("db is gone"),
        ),
    ):
        await volatility_gate.check_volatility(Symbol.XAUUSD)  # must not raise


@pytest.mark.asyncio
async def test_volatility_gate_null_spread_skips_ratio_check():
    """When spread is unavailable, only ATR check runs — no spread/ratio block."""
    from app.services import volatility_gate

    with (
        patch.object(settings, "volatility_gate_enabled", True),
        patch.object(settings, "atr_min_xauusd", 0.30),
        patch(
            "app.services.volatility_gate._compute_atr_m5",
            new=AsyncMock(return_value=1.50),
        ),
        patch(
            "app.services.volatility_gate._get_latest_spread",
            new=AsyncMock(return_value=None),  # no spread available
        ),
        patch("app.data.storage.log_execution_event", new=AsyncMock()),
    ):
        await volatility_gate.check_volatility(Symbol.XAUUSD)  # must not raise
