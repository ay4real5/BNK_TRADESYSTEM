"""
News blackout gate — high-impact event protection.

Blocks new entries within `news_blackout_minutes` (default 15) of any
scheduled high-impact economic event.

Events are persisted in a JSON file (data/news_events.json) so they
survive restarts.  The file can be managed via the API:
  GET  /news/events
  POST /news/events        — add a new event
  DELETE /news/events/{id} — remove an event

Design decisions:
  - Fail-OPEN if the file cannot be read (blocked=False, so trading continues)
  - Events are identified by integer IDs assigned at insert time
  - Timestamps are stored as ISO-8601 UTC strings
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from ..config import settings


# ---------------------------------------------------------------------------
# Persistence helpers (JSON file)
# ---------------------------------------------------------------------------

def _load_events() -> list[dict]:
    """Read events from the JSON file.  Returns [] on any error."""
    path = Path(settings.news_events_file)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("news_filter: could not read {}: {}", path, exc)
        return []


def _save_events(events: list[dict]) -> None:
    """Persist events list to the JSON file."""
    path = Path(settings.news_events_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(events, indent=2))
    except Exception as exc:
        logger.error("news_filter: could not write {}: {}", path, exc)


def _next_id(events: list[dict]) -> int:
    if not events:
        return 1
    return max(e.get("id", 0) for e in events) + 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def is_news_window(buffer_minutes: int | None = None) -> tuple[bool, str]:
    """
    Return (blocked: bool, reason: str).

    blocked=True means trading should be paused due to upcoming/recent news.
    buffer_minutes defaults to settings.news_blackout_minutes.
    """
    buf = buffer_minutes if buffer_minutes is not None else settings.news_blackout_minutes
    now = datetime.utcnow()
    events = _load_events()

    for evt in events:
        try:
            start = datetime.fromisoformat(evt["start"])
            end   = datetime.fromisoformat(evt.get("end", evt["start"]))
        except (KeyError, ValueError):
            continue
        window_start = start - timedelta(minutes=buf)
        window_end   = end   + timedelta(minutes=buf)
        if window_start <= now <= window_end:
            desc = evt.get("description", "High-impact event")
            reason = (
                f"news_blackout: {desc} "
                f"(window {window_start.strftime('%H:%M')}\u2013{window_end.strftime('%H:%M')} UTC)"
            )
            logger.info("news_filter BLOCK: {}", reason)
            await _log_block(reason)
            return True, reason

    return False, ""


def add_event(
    start: datetime,
    *,
    end: datetime | None = None,
    description: str = "High-impact event",
) -> dict:
    """
    Add a news event to the persistent list.
    Returns the newly created event dict.
    """
    events = _load_events()
    evt: dict[str, Any] = {
        "id": _next_id(events),
        "description": description,
        "start": start.isoformat(),
        "end":   (end or start).isoformat(),
        "created_at": datetime.utcnow().isoformat(),
    }
    events.append(evt)
    # Prune events that ended > 24 h ago to keep the file tidy
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    events = [e for e in events if e.get("end", e.get("start", "")) >= cutoff]
    _save_events(events)
    logger.info("news_filter: added event #{} \u2014 {}", evt["id"], description)
    return evt


def remove_event(event_id: int) -> bool:
    """Remove an event by ID.  Returns True if found and removed."""
    events = _load_events()
    before = len(events)
    events = [e for e in events if e.get("id") != event_id]
    if len(events) == before:
        return False
    _save_events(events)
    logger.info("news_filter: removed event #{}", event_id)
    return True


def list_events() -> list[dict]:
    """Return all scheduled events (sorted by start time)."""
    events = _load_events()
    return sorted(events, key=lambda e: e.get("start", ""))


def clear_expired() -> int:
    """Remove events whose end time has already passed.  Returns count removed."""
    now_str = datetime.utcnow().isoformat()
    events = _load_events()
    kept = [e for e in events if e.get("end", e.get("start", "")) >= now_str]
    removed = len(events) - len(kept)
    if removed:
        _save_events(kept)
        logger.info("news_filter: pruned {} expired event(s)", removed)
    return removed


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

async def _log_block(detail: str) -> None:
    try:
        from ..data.storage import log_execution_event
        await log_execution_event("news_block", detail=detail[:500])
    except Exception:
        pass

