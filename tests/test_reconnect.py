"""
tests/test_reconnect.py

Tests for the TCP socket auto-reconnect logic in get_trading_connection().

The cTrader broker silently drops idle connections after inactivity.
Before the fix, get_trading_connection() returned the stale singleton
directly, causing sync_positions to raise BrokerError → errors:1.

These tests verify the three drop scenarios are all detected correctly
and that a fresh connection is created transparently.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal stubs for asyncio.StreamWriter / StreamReader
# ---------------------------------------------------------------------------

class _FakeWriter:
    """Minimal mock for asyncio.StreamWriter."""
    def __init__(self, *, is_closing: bool = False) -> None:
        self._is_closing = is_closing

    def is_closing(self) -> bool:
        return self._is_closing


class _FakeReader:
    """Minimal mock for asyncio.StreamReader."""
    def __init__(self, *, at_eof: bool = False) -> None:
        self._at_eof = at_eof

    def at_eof(self) -> bool:
        return self._at_eof


# ---------------------------------------------------------------------------
# is_connected property — three drop scenarios
# ---------------------------------------------------------------------------

def test_is_connected_healthy():
    """Open writer + non-EOF reader → connection is healthy."""
    from app.integration.ctrader_trading import CTraderTradingConnection

    conn = object.__new__(CTraderTradingConnection)
    conn.writer = _FakeWriter(is_closing=False)
    conn.reader = _FakeReader(at_eof=False)
    conn._authenticated_accounts = {}

    assert conn.is_connected is True


def test_is_connected_no_writer():
    """No writer → connection is dead (scenario: never connected)."""
    from app.integration.ctrader_trading import CTraderTradingConnection

    conn = object.__new__(CTraderTradingConnection)
    conn.writer = None
    conn.reader = None
    conn._authenticated_accounts = {}

    assert conn.is_connected is False


def test_is_connected_writer_closing():
    """writer.is_closing() → connection is dead (broker reset scenario)."""
    from app.integration.ctrader_trading import CTraderTradingConnection

    conn = object.__new__(CTraderTradingConnection)
    conn.writer = _FakeWriter(is_closing=True)
    conn.reader = _FakeReader(at_eof=False)
    conn._authenticated_accounts = {}

    assert conn.is_connected is False


def test_is_connected_reader_at_eof():
    """reader.at_eof() → connection is dead (broker graceful close scenario)."""
    from app.integration.ctrader_trading import CTraderTradingConnection

    conn = object.__new__(CTraderTradingConnection)
    conn.writer = _FakeWriter(is_closing=False)
    conn.reader = _FakeReader(at_eof=True)
    conn._authenticated_accounts = {}

    assert conn.is_connected is False


# ---------------------------------------------------------------------------
# get_trading_connection() singleton lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_trading_connection_creates_fresh_when_none():
    """
    When no singleton exists, get_trading_connection creates and
    authenticates a brand-new connection.
    """
    import app.integration.ctrader_trading as _mod

    mock_conn = MagicMock()
    mock_conn.is_connected = True
    mock_conn.connect = AsyncMock()
    mock_conn.authenticate_application = AsyncMock()

    with (
        patch.object(_mod, "_trading_connection", None),
        patch(
            "app.integration.ctrader_trading.CTraderTradingConnection",
            return_value=mock_conn,
        ),
    ):
        result = await _mod.get_trading_connection()

    assert result is mock_conn
    mock_conn.connect.assert_awaited_once()
    mock_conn.authenticate_application.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_trading_connection_reuses_live_singleton():
    """
    When the singleton is alive, get_trading_connection returns it without
    any reconnect calls (no spurious teardown/reconnect).
    """
    import app.integration.ctrader_trading as _mod

    live_conn = MagicMock()
    live_conn.is_connected = True

    # Track that connect() is NOT called
    live_conn.connect = AsyncMock()
    live_conn.disconnect = AsyncMock()

    with patch.object(_mod, "_trading_connection", live_conn):
        result = await _mod.get_trading_connection()

    assert result is live_conn
    live_conn.connect.assert_not_awaited()
    live_conn.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_trading_connection_reconnects_after_broker_reset():
    """
    CRITICAL regression test: if the singleton's writer.is_closing() is True
    (broker reset the TCP socket), get_trading_connection() must:

      1. detect the dead connection (is_connected == False)
      2. call disconnect() to tear down cleanly
      3. create a NEW connection object
      4. call connect() + authenticate_application()
      5. return the new connection — not the stale one

    Before the fix: the stale connection was returned directly →
    sync_positions raised BrokerError("Not connected") → errors:1.
    """
    import app.integration.ctrader_trading as _mod

    # Simulate a dropped socket
    stale = MagicMock()
    stale.is_connected = False        # writer.is_closing() == True
    stale.disconnect = AsyncMock()

    fresh = MagicMock()
    fresh.is_connected = True
    fresh.connect = AsyncMock()
    fresh.authenticate_application = AsyncMock()

    with (
        patch.object(_mod, "_trading_connection", stale),
        patch(
            "app.integration.ctrader_trading.CTraderTradingConnection",
            return_value=fresh,
        ),
    ):
        result = await _mod.get_trading_connection()

    # Stale connection cleaned up
    stale.disconnect.assert_awaited_once()

    # Fresh connection fully initialised
    fresh.connect.assert_awaited_once()
    fresh.authenticate_application.assert_awaited_once()

    # Caller received the new healthy connection
    assert result is fresh
    assert result is not stale


@pytest.mark.asyncio
async def test_get_trading_connection_reconnects_after_remote_eof():
    """
    Variant: reader.at_eof() is True (remote graceful close) also
    triggers the same reconnect path.
    """
    import app.integration.ctrader_trading as _mod

    stale = MagicMock()
    stale.is_connected = False        # reader.at_eof() == True
    stale.disconnect = AsyncMock()

    fresh = MagicMock()
    fresh.is_connected = True
    fresh.connect = AsyncMock()
    fresh.authenticate_application = AsyncMock()

    with (
        patch.object(_mod, "_trading_connection", stale),
        patch(
            "app.integration.ctrader_trading.CTraderTradingConnection",
            return_value=fresh,
        ),
    ):
        result = await _mod.get_trading_connection()

    stale.disconnect.assert_awaited_once()
    fresh.connect.assert_awaited_once()
    assert result is fresh


@pytest.mark.asyncio
async def test_get_trading_connection_disconnect_error_is_swallowed():
    """
    If the stale connection's disconnect() throws, get_trading_connection
    must still proceed to create the fresh connection (no silent failure).
    """
    import app.integration.ctrader_trading as _mod

    stale = MagicMock()
    stale.is_connected = False
    stale.disconnect = AsyncMock(side_effect=OSError("already closed"))

    fresh = MagicMock()
    fresh.is_connected = True
    fresh.connect = AsyncMock()
    fresh.authenticate_application = AsyncMock()

    with (
        patch.object(_mod, "_trading_connection", stale),
        patch(
            "app.integration.ctrader_trading.CTraderTradingConnection",
            return_value=fresh,
        ),
    ):
        # Must NOT raise even though disconnect() threw
        result = await _mod.get_trading_connection()

    assert result is fresh
    fresh.connect.assert_awaited_once()
