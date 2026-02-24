"""
News filter service.

Stub implementation — returns False (no news block) initially.
Integrate a real economic calendar API (e.g., ForexFactory, Finnhub)
when ready for production.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger


# Hard-coded high-impact windows (UTC) — replace with live calendar feed
# Format: list of (start_utc, end_utc, description)
MANUAL_EVENTS: list[tuple[datetime, datetime, str]] = []


async def is_news_window(buffer_minutes: int = 30) -> tuple[bool, str]:
    """
    Return (blocked, reason).

    blocked=True means trading should be paused due to upcoming/recent news.
    """
    now = datetime.utcnow()
    for start, end, desc in MANUAL_EVENTS:
        window_start = start - timedelta(minutes=buffer_minutes)
        window_end = end + timedelta(minutes=buffer_minutes)
        if window_start <= now <= window_end:
            reason = f"📰 News window: {desc} ({start.strftime('%H:%M')} UTC)"
            logger.info("News filter blocking trade: {}", reason)
            return True, reason
    return False, ""


def add_news_event(start: datetime, end: datetime, description: str) -> None:
    """Manually add a high-impact news event to the filter."""
    MANUAL_EVENTS.append((start, end, description))
    logger.info("News event added: {} {} - {}", description, start, end)
