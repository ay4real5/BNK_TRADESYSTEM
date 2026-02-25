"""
app/integration/ctrader_data.py
================================
Phase 4: cTrader Open API — read-only market data integration.

Architecture
------------
CTraderFeed (public API)
  └── CTraderConnection   — pure-asyncio TCP/TLS client for the OpenAPI
  └── CTraderLiveProvider — DataProvider that serves the live candle buffer
  └── MultiTimeframeCandleBuilder (per symbol)

Protocol
--------
cTrader OpenAPI 2.0 uses length-prefixed Protobuf messages over TLS:
  [4 bytes big-endian length][Protobuf ProtoMessage bytes]

Message flow:
  → ProtoOAApplicationAuthReq
  ← ProtoOAApplicationAuthRes
  → ProtoOAAccountAuthReq
  ← ProtoOAAccountAuthRes
  → ProtoOASubscribeSpotsReq   (one per symbol)
  ← ProtoOASpotEvent           (streaming ticks)
  → ProtoOAGetTrendbarsReq     (historical warm-up)
  ← ProtoOAGetTrendbarsRes

SDK dependency
--------------
    pip install ctrader-open-api   # official Spotware protobuf messages

The SDK's Twisted client is NOT used here; only its generated protobuf Python
files are imported. The connection is implemented using native asyncio + ssl.

Configuration (.env)
--------------------
MARKET_DATA_SOURCE=ctrader         # "internal" or "ctrader"
CTRADER_CLIENT_ID=...
CTRADER_CLIENT_SECRET=...
CTRADER_ACCESS_TOKEN=...
CTRADER_ACCOUNT_ID=...
CTRADER_DEMO=true                  # true=demo host / false=live host

Read-only guarantee
-------------------
This module subscribes to market data only.
Order placement is in app/execution/ctrader.py (Phase 5, not yet active).
"""
from __future__ import annotations

import asyncio
import collections
import ssl
import struct
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from loguru import logger

from ..config import settings
from ..data import market_data, storage
from ..domain.enums import Symbol
from ..domain.errors import BrokerError, DataFetchError, InsufficientDataError
from ..domain.models import Candle
from ..data.market_data import DataProvider
from ..services import analyzer
from ..services.candle_builder import CandleBuilderService
from .candle_builder import MultiTimeframeCandleBuilder, Tick


# ---------------------------------------------------------------------------
# SDK lazy import (only generated message classes, not Twisted client)
# ---------------------------------------------------------------------------

def _import_sdk():
    """
    Try to import the generated protobuf message classes from ctrader-open-api.
    Raises ImportError with installation instructions if not available.
    """
    try:
        # noqa: these are heavy imports, done once at connection time only
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (  # type: ignore
            ProtoOAApplicationAuthReq,
            ProtoOAApplicationAuthRes,
            ProtoOAAccountAuthReq,
            ProtoOAAccountAuthRes,
            ProtoOASubscribeSpotsReq,
            ProtoOASubscribeSpotsRes,
            ProtoOAGetTrendbarsReq,
            ProtoOAGetTrendbarsRes,
            ProtoOASpotEvent,
            ProtoOAUnsubscribeSpotsReq,
            ProtoOAGetSymbolsReq,
            ProtoOAGetSymbolsRes,
            ProtoOASubscribeLiveTrendbarReq,
            ProtoOASubscribeLiveTrendbarRes,
            ProtoOALiveTrendbarEvent,
        )
        from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (  # type: ignore
            ProtoMessage,
        )
        from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (  # type: ignore
            ProtoOATrendbarPeriod,
        )
        return {
            "ProtoMessage":                      ProtoMessage,
            "ProtoOAApplicationAuthReq":         ProtoOAApplicationAuthReq,
            "ProtoOAApplicationAuthRes":         ProtoOAApplicationAuthRes,
            "ProtoOAAccountAuthReq":             ProtoOAAccountAuthReq,
            "ProtoOAAccountAuthRes":             ProtoOAAccountAuthRes,
            "ProtoOASubscribeSpotsReq":          ProtoOASubscribeSpotsReq,
            "ProtoOASubscribeSpotsRes":          ProtoOASubscribeSpotsRes,
            "ProtoOAGetTrendbarsReq":            ProtoOAGetTrendbarsReq,
            "ProtoOAGetTrendbarsRes":            ProtoOAGetTrendbarsRes,
            "ProtoOASpotEvent":                  ProtoOASpotEvent,
            "ProtoOAUnsubscribeSpotsReq":        ProtoOAUnsubscribeSpotsReq,
            "ProtoOAGetSymbolsReq":              ProtoOAGetSymbolsReq,
            "ProtoOAGetSymbolsRes":              ProtoOAGetSymbolsRes,
            "ProtoOASubscribeLiveTrendbarReq":   ProtoOASubscribeLiveTrendbarReq,
            "ProtoOASubscribeLiveTrendbarRes":   ProtoOASubscribeLiveTrendbarRes,
            "ProtoOALiveTrendbarEvent":          ProtoOALiveTrendbarEvent,
            "ProtoOATrendbarPeriod":             ProtoOATrendbarPeriod,
        }
    except ImportError as exc:
        raise ImportError(
            "cTrader Open API SDK not installed.\n"
            "Run:  pip install ctrader-open-api\n"
            "or add it to pyproject.toml dependencies.\n"
            f"Original error: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Symbol ID mapping (cTrader internal IDs for common instruments)
# Symbol IDs are broker and account-type specific.
# Pepperstone Spread Betting demo account (46435708) uses _SB variants.
# ---------------------------------------------------------------------------

_SYMBOL_ID_MAP: dict[Symbol, int] = {
    Symbol.XAUUSD: 241,   # XAUUSD_SB (Gold - Pepperstone Spread Betting)
    Symbol.XAGUSD: 238,   # XAGUSD_SB (Silver - Pepperstone Spread Betting)
}

# cTrader periodicity codes (ProtoOATrendbarPeriod enum values)
_TF_PERIOD_MAP: dict[str, int] = {
    "1m":  1,    # M1
    "2m":  2,    # M2
    "3m":  3,    # M3
    "5m":  5,    # M5
    "10m": 10,   # M10
    "15m": 15,   # M15
    "30m": 30,   # M30
    "1h":  60,   # H1
    "4h":  240,  # H4
    "1d":  1440, # D1
}


# ---------------------------------------------------------------------------
# CTraderLiveProvider — DataProvider backed by a live candle ring buffer
# ---------------------------------------------------------------------------

class CTraderLiveProvider(DataProvider):
    """
    Implements the DataProvider interface.
    Candles are pushed in real-time by CTraderConnection and buffered here.
    The analyzer fetches candles via market_data.fetch_candles() which
    calls this provider's fetch_candles().
    """

    def __init__(self, buffer_size: int = 500) -> None:
        self._buffer_size = buffer_size
        # {(symbol, timeframe) → deque[Candle]}
        self._candles: dict[tuple[Symbol, str], collections.deque] = {}
        # {symbol → float}
        self._prices:  dict[Symbol, float] = {}
        self._spreads: dict[Symbol, float] = {}
        self._last_tick_ts: dict[Symbol, datetime] = {}
        self._data_source: str = "ctrader_live"
        # {symbol → list[asyncio.Queue]}  — for stream_ticks() subscribers
        self._tick_queues: dict[Symbol, list[asyncio.Queue]] = {}

    # ── Push interface (called by CTraderConnection) ───────────────────

    def push_candle(self, candle: Candle) -> None:
        key = (candle.symbol, candle.timeframe)
        if key not in self._candles:
            self._candles[key] = collections.deque(maxlen=self._buffer_size)
        self._candles[key].append(candle)
        logger.debug(
            "LiveProvider: {} {} {} O={} H={} L={} C={}",
            candle.symbol.value, candle.timeframe, candle.ts.isoformat(),
            candle.open, candle.high, candle.low, candle.close,
        )

    def push_tick(self, symbol: Symbol, bid: float, ask: float) -> None:
        mid = (bid + ask) / 2.0
        self._prices[symbol]   = mid
        self._spreads[symbol]  = ask - bid
        self._last_tick_ts[symbol] = datetime.now(tz=timezone.utc)
        # Notify any stream_ticks() async generators
        for q in self._tick_queues.get(symbol, []):
            q.put_nowait((bid, ask, datetime.now(tz=timezone.utc)))

    # ── DataProvider interface ─────────────────────────────────────────

    async def fetch_candles(
        self,
        symbol: Symbol,
        timeframe: str,
        count: int = 300,
    ) -> list[Candle]:
        key = (symbol, timeframe)
        if key not in self._candles or not self._candles[key]:
            raise InsufficientDataError(
                f"No live candles yet for {symbol.value} {timeframe}. "
                "Waiting for cTrader stream warm-up (usually <60 s)."
            )
        buf = self._candles[key]
        candles = list(buf)[-count:]
        if len(candles) < 50:
            raise InsufficientDataError(
                f"Only {len(candles)} candles buffered for {symbol.value} {timeframe}; "
                f"need at least 50 for analysis."
            )
        return candles

    async def fetch_price(self, symbol: Symbol) -> float:
        if symbol not in self._prices:
            raise DataFetchError(f"No live price yet for {symbol.value}.")
        return self._prices[symbol]

    async def fetch_spread(self, symbol: Symbol) -> float:
        if symbol not in self._spreads:
            return 0.30  # Conservative default until first tick
        return self._spreads[symbol]

    # ── Diagnostics ───────────────────────────────────────────────────

    def candle_counts(self) -> dict[str, int]:
        return {
            f"{sym.value}/{tf}": len(buf)
            for (sym, tf), buf in self._candles.items()
        }

    def last_tick_age_seconds(self, symbol: Symbol) -> float | None:
        ts = self._last_tick_ts.get(symbol)
        if ts is None:
            return None
        return (datetime.now(tz=timezone.utc) - ts).total_seconds()

    # ── Simple tick access interface ───────────────────────────────────

    async def get_latest_tick(self, symbol: Symbol) -> tuple[float, float] | None:
        """
        Return (bid, ask) for the most recent tick, or None if not yet received.
        """
        bid_ask = (
            self._prices.get(symbol),
            self._prices.get(symbol),
        )
        if bid_ask[0] is None:
            return None
        spread = self._spreads.get(symbol, 0.0)
        mid    = self._prices[symbol]
        half   = spread / 2.0
        return (round(mid - half, 6), round(mid + half, 6))

    async def stream_ticks(
        self,
        symbol: Symbol,
        stop_event: asyncio.Event | None = None,
    ):
        """
        Async generator: yields (bid, ask, ts) tuples as ticks arrive.
        Stops when stop_event is set or the generator is closed.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        if symbol not in self._tick_queues:
            self._tick_queues[symbol] = []
        self._tick_queues[symbol].append(q)
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    item = await asyncio.wait_for(q.get(), timeout=1.0)
                    yield item
                except asyncio.TimeoutError:
                    continue
        finally:
            self._tick_queues[symbol].remove(q)


# ---------------------------------------------------------------------------
# CTraderConnection — native asyncio TLS TCP client
# ---------------------------------------------------------------------------

# cTrader API hosts
_DEMO_HOST = "demo.ctraderapi.com"
_LIVE_HOST = "live.ctraderapi.com"
_API_PORT  = 5035

# Reconnection strategy
_RECONNECT_DELAYS = [5, 10, 30, 60, 120]  # seconds, then caps at 120s


class CTraderConnection:
    """
    Low-level asyncio TLS TCP client for the cTrader Open API 2.0.

    Message framing:  [uint32 big-endian length][protobuf ProtoMessage bytes]

    This class handles:
     - TLS connection establishment
     - Application + account authentication sequence
     - Symbol ID resolution
     - Spot subscription (ticks)
     - Live trendbar subscription (server-side candle events)
     - Historical candle warm-up
     - Heartbeat / keepalive (every 25 seconds)
     - Reconnection with exponential back-off
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        access_token: str,
        account_id: str,
        symbols: list[Symbol],
        live_provider: CTraderLiveProvider,
        is_demo: bool = True,
        timeframes_minutes: list[int] | None = None,
    ) -> None:
        self.client_id    = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id   = int(account_id)
        self.symbols      = symbols
        self.provider     = live_provider
        self.host         = _DEMO_HOST if is_demo else _LIVE_HOST
        self.port         = _API_PORT
        self.timeframes   = timeframes_minutes or [1, 15, 60]

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

        self._symbol_id_map: dict[Symbol, int] = {}
        self._candle_builders: dict[Symbol, MultiTimeframeCandleBuilder] = {}

        self._stop_event = asyncio.Event()
        self._reconnect_count = 0
        self._last_heartbeat = 0.0

        # Payload-type → protobuf class map (populated in _connect)
        self._pb: dict[str, Any] = {}

        # SQLite persistence service (handles ticks + M1/M5 candles)
        self._candle_svc = CandleBuilderService(
            timeframes_minutes=[1, 5],
            persist_ticks=True,
        )

        # Statistics
        self.stats = {
            "ticks_received":    0,
            "candles_completed": 0,
            "reconnects":        0,
            "last_candle_ts":    None,
            "connected_since":   None,
            "last_tick_ts":      None,
        }

    # ── Public control ────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin connecting; runs until stop() is called."""
        self._stop_event.clear()
        asyncio.create_task(self._run_with_reconnect())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

    # ── Connection lifecycle ──────────────────────────────────────────

    async def _run_with_reconnect(self) -> None:
        delay_idx = 0
        while not self._stop_event.is_set():
            try:
                await self._connect_and_run()
                delay_idx = 0   # reset on clean run
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._reconnect_count += 1
                self.stats["reconnects"] += 1
                delay = _RECONNECT_DELAYS[min(delay_idx, len(_RECONNECT_DELAYS) - 1)]
                delay_idx += 1
                logger.warning(
                    "cTrader connection lost (attempt {}): {}. Retrying in {}s …",
                    self._reconnect_count, exc, delay,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def _connect_and_run(self) -> None:
        """Open TLS connection, authenticate, subscribe, then pump messages."""
        self._pb = _import_sdk()
        logger.info(
            "cTrader: connecting to {}:{} ({}) …",
            self.host, self.port,
            "DEMO" if self.host == _DEMO_HOST else "LIVE",
        )

        ssl_ctx = ssl.create_default_context()
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ssl_ctx),
            timeout=30,
        )
        logger.info("cTrader: TCP/TLS connected")

        # Auth sequence
        await self._app_auth()
        await self._account_auth()
        logger.info("cTrader: authenticated (accountId={})", self.account_id)

        # Resolve symbol IDs (or use built-in map)
        await self._resolve_symbols()

        # Warm up historical candles
        for symbol in self.symbols:
            for tf in self.timeframes:
                await self._fetch_history(symbol, tf)

        # Subscribe to live trendbars + spot ticks
        for symbol in self.symbols:
            await self._subscribe_trendbars(symbol)
            await self._subscribe_spots(symbol)

        self.stats["connected_since"] = datetime.now(tz=timezone.utc).isoformat()
        logger.info(
            "cTrader: live subscription active for {} symbol(s): {}",
            len(self.symbols), [s.value for s in self.symbols],
        )

        # Main message pump
        await self._message_pump()

    # ── Authentication ────────────────────────────────────────────────

    async def _app_auth(self) -> None:
        req = self._pb["ProtoOAApplicationAuthReq"]()
        req.clientId     = self.client_id
        req.clientSecret = self.client_secret
        await self._send(req, self._pb["ProtoOAApplicationAuthReq"].DESCRIPTOR.full_name)
        resp = await self._recv_typed("ProtoOAApplicationAuthRes")
        logger.debug("cTrader: app auth OK — {}", resp)

    async def _account_auth(self) -> None:
        req = self._pb["ProtoOAAccountAuthReq"]()
        req.ctidTraderAccountId = self.account_id
        req.accessToken         = self.access_token
        await self._send(req, self._pb["ProtoOAAccountAuthReq"].DESCRIPTOR.full_name)
        resp = await self._recv_typed("ProtoOAAccountAuthRes")
        logger.debug("cTrader: account auth OK — {}", resp)

    # ── Symbol resolution ─────────────────────────────────────────────

    async def _resolve_symbols(self) -> None:
        """
        Query broker's symbol list and map our Symbol enum to cTrader symbolIds.
        Falls back to the built-in _SYMBOL_ID_MAP if query fails.
        """
        try:
            req = self._pb["ProtoOAGetSymbolsReq"]()
            req.ctidTraderAccountId = self.account_id
            await self._send(req, req.DESCRIPTOR.full_name)
            resp = await self._recv_typed("ProtoOAGetSymbolsRes", timeout=15)

            # Build a name → id map from the broker's symbol list
            name_to_id: dict[str, int] = {}
            for sym_info in resp.symbol:
                name_to_id[sym_info.symbolName] = sym_info.symbolId

            for sym in self.symbols:
                # Try exact match, then without slash
                for candidate in (sym.value, sym.value.replace("/", "")):
                    if candidate in name_to_id:
                        self._symbol_id_map[sym] = name_to_id[candidate]
                        logger.info(
                            "cTrader: resolved {} → symbolId={}",
                            sym.value, name_to_id[candidate],
                        )
                        break
                else:
                    # Fall back to hard-coded map
                    if sym in _SYMBOL_ID_MAP:
                        self._symbol_id_map[sym] = _SYMBOL_ID_MAP[sym]
                        logger.warning(
                            "cTrader: {} not in broker symbol list; "
                            "using default symbolId={}",
                            sym.value, _SYMBOL_ID_MAP[sym],
                        )

        except Exception as exc:
            logger.warning(
                "cTrader: symbol resolution failed ({}); using default symbol IDs", exc
            )
            self._symbol_id_map = {s: _SYMBOL_ID_MAP[s] for s in self.symbols if s in _SYMBOL_ID_MAP}

    # ── Historical warm-up ────────────────────────────────────────────

    async def _fetch_history(self, symbol: Symbol, timeframe_minutes: int) -> None:
        """
        Pull the last ~300 candles for (symbol, timeframe) to seed the
        live provider buffer before streaming begins.
        """
        tf_name = f"{timeframe_minutes}m"
        period  = _TF_PERIOD_MAP.get(tf_name)
        if period is None:
            return
        sym_id = self._symbol_id_map.get(symbol)
        if sym_id is None:
            logger.warning("cTrader: no symbolId for {} — skipping history warmup", symbol.value)
            return

        count        = 300
        to_ts_ms     = int(time.time() * 1000)
        from_ts_ms   = to_ts_ms - count * timeframe_minutes * 60 * 1000

        req = self._pb["ProtoOAGetTrendbarsReq"]()
        req.ctidTraderAccountId = self.account_id
        req.symbolId            = sym_id
        req.period              = period
        req.fromTimestamp       = from_ts_ms
        req.toTimestamp         = to_ts_ms
        req.count               = count
        await self._send(req, req.DESCRIPTOR.full_name)

        try:
            resp = await self._recv_typed("ProtoOAGetTrendbarsRes", timeout=20)
            bars = resp.trendbar
            if not bars:
                logger.info(
                    "cTrader: history warmup {} {}: 0 bars returned", symbol.value, tf_name
                )
                return
            for bar in bars:
                ts_dt = datetime.fromtimestamp(bar.utcTimestampInMinutes * 60, tz=timezone.utc)
                # cTrader returns prices in 1/100000 (pipette) format
                precision = getattr(resp, "symbolDigits", 5) or 5
                divisor   = 10 ** precision
                candle = Candle(
                    ts        = ts_dt,
                    symbol    = symbol,
                    timeframe = tf_name,
                    open      = bar.low / divisor + bar.deltaOpen / divisor,
                    high      = bar.low / divisor + bar.deltaHigh / divisor,
                    low       = bar.low / divisor,
                    close     = bar.low / divisor + bar.deltaClose / divisor,
                    volume    = bar.volume,
                )
                self.provider.push_candle(candle)
            logger.info(
                "cTrader: history warmup {} {}: {} candles loaded",
                symbol.value, tf_name, len(bars),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "cTrader: history warmup timed out for {} {}", symbol.value, tf_name
            )

    # ── Subscriptions ─────────────────────────────────────────────────

    async def _subscribe_spots(self, symbol: Symbol) -> None:
        sym_id = self._symbol_id_map.get(symbol)
        if sym_id is None:
            return
        req = self._pb["ProtoOASubscribeSpotsReq"]()
        req.ctidTraderAccountId = self.account_id
        req.symbolId.append(sym_id)
        await self._send(req, req.DESCRIPTOR.full_name)
        logger.info("cTrader: subscribed to spot ticks for {}", symbol.value)

    async def _subscribe_trendbars(self, symbol: Symbol) -> None:
        """Subscribe to server-side live trendbar events (alternative to tick aggregation)."""
        sym_id = self._symbol_id_map.get(symbol)
        if sym_id is None:
            return
        for tf_minutes in self.timeframes:
            tf_name = f"{tf_minutes}m"
            period  = _TF_PERIOD_MAP.get(tf_name)
            if period is None:
                continue
            try:
                req = self._pb["ProtoOASubscribeLiveTrendbarReq"]()
                req.ctidTraderAccountId = self.account_id
                req.symbolId            = sym_id
                req.period              = period
                await self._send(req, req.DESCRIPTOR.full_name)
                logger.debug(
                    "cTrader: subscribed to live trendbars {} {}", symbol.value, tf_name
                )
            except Exception as exc:
                logger.warning(
                    "cTrader: live trendbar subscription failed {} {}: {}",
                    symbol.value, tf_name, exc,
                )

    # ── Message pump ─────────────────────────────────────────────────

    async def _message_pump(self) -> None:
        """
        Read messages indefinitely, dispatching to handlers.
        Also sends keepalive heartbeats every 25 seconds.
        """
        logger.info("cTrader: message pump running …")
        while not self._stop_event.is_set():
            # Heartbeat check
            now = time.monotonic()
            if now - self._last_heartbeat > 25:
                await self._send_heartbeat()
                self._last_heartbeat = now

            try:
                msg = await asyncio.wait_for(self._recv_raw(), timeout=30)
            except asyncio.TimeoutError:
                continue    # just loop and send heartbeat

            await self._dispatch(msg)

    async def _dispatch(self, raw_payload: bytes) -> None:
        """Route a decoded ProtoMessage payload to the correct handler."""
        pb = self._pb
        try:
            proto_msg = pb["ProtoMessage"]()
            proto_msg.ParseFromString(raw_payload)
            payload_type = proto_msg.payloadType

            # Spot event (real-time tick)
            if payload_type == _payload_type_for(
                pb["ProtoOASpotEvent"].DESCRIPTOR.full_name
            ):
                event = pb["ProtoOASpotEvent"]()
                event.ParseFromString(proto_msg.payload)
                await self._on_spot_event(event)

            # Live trendbar event (server-side closed candle)
            elif payload_type == _payload_type_for(
                pb["ProtoOALiveTrendbarEvent"].DESCRIPTOR.full_name
            ):
                event = pb["ProtoOALiveTrendbarEvent"]()
                event.ParseFromString(proto_msg.payload)
                await self._on_live_trendbar_event(event)

            # Heartbeat response (ignore)
            # Error responses
            else:
                pass  # Unhandled payload types silently ignored
        except Exception as exc:
            logger.debug("cTrader dispatch error: {}", exc)

    # ── Spot event handler ─────────────────────────────────────────────

    async def _on_spot_event(self, event: Any) -> None:
        """Convert a real-time spot event to tick; feed candle builders."""
        sym_id = event.symbolId
        symbol  = self._id_to_symbol(sym_id)
        if symbol is None:
            return

        # Prices are in 1/100000 of base currency
        precision = getattr(event, "symbolDigits", 5) or 5
        divisor   = 10 ** precision
        bid = event.bid / divisor if event.bid else 0.0
        ask = event.ask / divisor if event.ask else (bid + 0.0003)

        now_ts = datetime.now(tz=timezone.utc)
        self.provider.push_tick(symbol, bid, ask)
        self.stats["ticks_received"] += 1
        self.stats["last_tick_ts"]    = now_ts.isoformat()

        tick = Tick(ts=now_ts, bid=bid, ask=ask)

        # ── In-memory candle builders (all configured timeframes) ──────
        if symbol not in self._candle_builders:
            self._candle_builders[symbol] = MultiTimeframeCandleBuilder(
                symbol, self.timeframes
            )
        builder = self._candle_builders[symbol]
        closed_bars = builder.on_tick(tick)

        for tf_minutes, candle in closed_bars.items():
            self.provider.push_candle(candle)
            self.stats["candles_completed"] += 1
            self.stats["last_candle_ts"] = candle.ts.isoformat()
            logger.debug(
                "cTrader tick→candle: {} {}m closed at {}",
                symbol.value, tf_minutes, candle.ts.isoformat(),
            )
            # Trigger analysis after each 1m candle close
            if tf_minutes == 1:
                asyncio.create_task(self._trigger_analysis())

        # ── SQLite persistence (M1 + M5 candles + raw ticks) ──────────
        asyncio.create_task(
            self._candle_svc.process_tick(symbol.value, bid, ask, now_ts),
            name=f"candle_svc_{symbol.value}",
        )

    # ── Live trendbar handler ─────────────────────────────────────────

    async def _on_live_trendbar_event(self, event: Any) -> None:
        """Server-side completed candle → push directly to live provider."""
        sym_id = event.symbolId
        symbol  = self._id_to_symbol(sym_id)
        if symbol is None:
            return

        bar    = event.trendbar
        period = event.period if hasattr(event, "period") else None
        tf_name = _period_to_tf(period)
        if tf_name is None:
            return

        precision = 5
        divisor   = 10 ** precision
        open_  = bar.low / divisor + bar.deltaOpen  / divisor
        high   = bar.low / divisor + bar.deltaHigh  / divisor
        low_   = bar.low / divisor
        close  = bar.low / divisor + bar.deltaClose / divisor
        ts_dt  = datetime.fromtimestamp(bar.utcTimestampInMinutes * 60, tz=timezone.utc)

        candle = Candle(
            ts=ts_dt, symbol=symbol, timeframe=tf_name,
            open=open_, high=high, low=low_, close=close,
            volume=bar.volume,
        )
        self.provider.push_candle(candle)
        self.stats["candles_completed"] += 1
        self.stats["last_candle_ts"] = ts_dt.isoformat()

        # Trigger analysis on 1m close
        if tf_name == "1m":
            asyncio.create_task(self._trigger_analysis())

    # ── Analysis trigger ──────────────────────────────────────────────

    async def _trigger_analysis(self) -> None:
        """
        Run the full analysis cycle after a 1-minute bar close.
        Signals are stored in DB exactly as the demo engine does.
        """
        try:
            ideas = await analyzer.run_analysis_cycle()
            if ideas:
                logger.info(
                    "cTrader analysis: {} signal(s) generated: {}",
                    len(ideas),
                    [f"{i.symbol.value} {i.side.value} score={i.score}" for i in ideas],
                )
        except Exception as exc:
            logger.debug("cTrader analysis cycle error: {}", exc)

    # ── Wire protocol helpers ─────────────────────────────────────────

    async def _send(self, message: Any, type_name: str) -> None:
        """Wrap message in ProtoMessage envelope and send."""
        pb = self._pb
        payload       = message.SerializeToString()
        proto_msg     = pb["ProtoMessage"]()
        proto_msg.payloadType = _payload_type_for(type_name)
        proto_msg.payload     = payload
        framed = proto_msg.SerializeToString()
        header = struct.pack(">I", len(framed))
        self._writer.write(header + framed)
        await self._writer.drain()

    async def _send_heartbeat(self) -> None:
        pb  = self._pb
        msg = pb["ProtoMessage"]()
        msg.payloadType = 51   # PING
        framed = msg.SerializeToString()
        header = struct.pack(">I", len(framed))
        try:
            self._writer.write(header + framed)
            await self._writer.drain()
        except Exception:
            pass

    async def _recv_raw(self) -> bytes:
        """Read exactly one framed message and return the ProtoMessage bytes."""
        header = await self._reader.readexactly(4)
        length = struct.unpack(">I", header)[0]
        if length > 10 * 1024 * 1024:
            raise BrokerError(f"cTrader: oversized message ({length} bytes)")
        return await self._reader.readexactly(length)

    async def _recv_typed(self, class_name: str, timeout: float = 10.0) -> Any:
        """Read messages until one matching class_name is received."""
        pb = self._pb
        target_type = _payload_type_for(pb[class_name].DESCRIPTOR.full_name)
        deadline    = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"Timed out waiting for {class_name}")
            raw = await asyncio.wait_for(self._recv_raw(), timeout=remaining)
            proto_msg = pb["ProtoMessage"]()
            proto_msg.ParseFromString(raw)
            if proto_msg.payloadType == target_type:
                msg = pb[class_name]()
                msg.ParseFromString(proto_msg.payload)
                return msg
            # Otherwise dispatch as side-effect and keep waiting
            await self._dispatch(raw)

    # ── Utilities ─────────────────────────────────────────────────────

    def _id_to_symbol(self, sym_id: int) -> Symbol | None:
        for sym, sid in self._symbol_id_map.items():
            if sid == sym_id:
                return sym
        return None


# ---------------------------------------------------------------------------
# Payload type mapping  (numeric ID for each protobuf message class)
# ---------------------------------------------------------------------------

# The cTrader protobuf spec assigns a numeric payloadType to each message.
# ProtoMessage.payloadType is set to the numeric extension field value.
# For simplicity, we use a deterministic hash based on the full_name string.
# In practice these are defined as enum values in the .proto files.
_PAYLOAD_TYPE_CACHE: dict[str, int] = {
    "ProtoOAApplicationAuthReq":       2100,
    "ProtoOAApplicationAuthRes":       2101,
    "ProtoOAAccountAuthReq":           2102,
    "ProtoOAAccountAuthRes":           2103,
    "ProtoOAGetSymbolsReq":            2115,
    "ProtoOAGetSymbolsRes":            2116,
    "ProtoOASubscribeSpotsReq":        2120,
    "ProtoOASubscribeSpotsRes":        2121,
    "ProtoOAUnsubscribeSpotsReq":      2122,
    "ProtoOASpotEvent":                2131,
    "ProtoOAGetTrendbarsReq":          2137,
    "ProtoOAGetTrendbarsRes":          2138,
    "ProtoOASubscribeLiveTrendbarReq": 2165,
    "ProtoOASubscribeLiveTrendbarRes": 2166,
    "ProtoOALiveTrendbarEvent":        2167,
}


def _payload_type_for(full_or_short_name: str) -> int:
    """
    Return the numeric payloadType for a given message class.
    Tries the short class name first, then the full.protobuf.name.
    """
    short = full_or_short_name.rsplit(".", 1)[-1]
    return _PAYLOAD_TYPE_CACHE.get(short, 0)


def _period_to_tf(period: int | None) -> str | None:
    """Convert cTrader period integer back to timeframe string."""
    if period is None:
        return None
    for tf, p in _TF_PERIOD_MAP.items():
        if p == period:
            return tf
    return None


# ---------------------------------------------------------------------------
# CTraderFeed — top-level facade used by server.py
# ---------------------------------------------------------------------------

class CTraderFeed:
    """
    High-level facade.  Start/stop via server lifespan.

    Settings read from app.config:
      MARKET_DATA_SOURCE   (must be "ctrader")
      CTRADER_CLIENT_ID
      CTRADER_CLIENT_SECRET
      CTRADER_ACCESS_TOKEN
      CTRADER_ACCOUNT_ID
      CTRADER_DEMO         ("true" / "false", default true)
    """

    def __init__(self) -> None:
        self._connection: CTraderConnection | None = None
        self.live_provider = CTraderLiveProvider()
        self._started = False

    def build(
        self,
        client_id: str,
        client_secret: str,
        access_token: str,
        account_id: str,
        symbols: list[Symbol],
        is_demo: bool = True,
    ) -> "CTraderFeed":
        tf_minutes = _entry_timeframes_minutes()
        self._connection = CTraderConnection(
            client_id       = client_id,
            client_secret   = client_secret,
            access_token    = access_token,
            account_id      = account_id,
            symbols         = symbols,
            live_provider   = self.live_provider,
            is_demo         = is_demo,
            timeframes_minutes = tf_minutes,
        )
        return self

    async def start(self) -> None:
        if self._connection is None:
            raise RuntimeError("Call CTraderFeed.build() before start()")
        await self._connection.start()
        # Register live provider as the active market data provider
        market_data.set_provider(self.live_provider)
        self._started = True
        logger.info(
            "CTraderFeed started — data source switched to ctrader_live"
        )

    async def stop(self) -> None:
        if self._connection:
            await self._connection.stop()
        self._started = False
        logger.info("CTraderFeed stopped")

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def stats(self) -> dict:
        if self._connection is None:
            return {}
        s = dict(self._connection.stats)
        s["candle_buffer_counts"] = self.live_provider.candle_counts()
        return s

    # ── Factory classmethod ────────────────────────────────────────────

    @classmethod
    def from_settings(cls) -> "CTraderFeed":
        """
        Build a CTraderFeed from app.config settings.
        Raises ValueError if required credentials are missing.
        """
        creds = {
            "client_id":     settings.ctrader_client_id,
            "client_secret": settings.ctrader_client_secret,
            "access_token":  settings.ctrader_access_token,
            "account_id":    settings.ctrader_account_id,
        }
        missing = [k for k, v in creds.items() if not v]
        if missing:
            raise ValueError(
                f"cTrader credentials missing from .env: "
                f"{', '.join(missing).upper()}\n"
                "Set CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET, "
                "CTRADER_ACCESS_TOKEN, CTRADER_ACCOUNT_ID."
            )
        is_demo = str(getattr(settings, "ctrader_demo", "true")).lower() in ("true", "1", "yes")
        return cls().build(
            **creds,
            symbols = settings.active_symbols,
            is_demo = is_demo,
        )


# ---------------------------------------------------------------------------
# Module-level singleton (lazily populated by server.py)
# ---------------------------------------------------------------------------

ctrader_feed: CTraderFeed | None = None


def get_feed() -> CTraderFeed | None:
    return ctrader_feed


def set_feed(feed: CTraderFeed) -> None:
    global ctrader_feed
    ctrader_feed = feed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry_timeframes_minutes() -> list[int]:
    """
    Return a sorted list of timeframe minute-widths we need candles for.
    Always includes 1 (for tick→candle builders) plus entry_tf and bias_tf.
    """
    tfs = {1}
    for tf in (settings.entry_tf, settings.bias_tf):
        if tf.endswith("m"):
            tfs.add(int(tf[:-1]))
        elif tf.endswith("h"):
            tfs.add(int(tf[:-1]) * 60)
    return sorted(tfs)
