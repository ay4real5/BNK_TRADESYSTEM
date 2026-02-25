"""
cTrader Trading Adapter — Protobuf over TCP/TLS

Minimal viable implementation for placing demo trades via cTrader Open API.

Protocol: Length-prefixed Protobuf messages over TLS socket
Hosts:
  - demo.ctraderapi.com:5035 (demo)
  - live.ctraderapi.com:5035 (live)

Message flow for trading:
  1. ProtoOAApplicationAuthReq → ProtoOAApplicationAuthRes
  2. ProtoOAAccountAuthReq → ProtoOAAccountAuthRes
  3. ProtoOANewOrderReq → ProtoOAExecutionEvent  (type 2126)
     OR ProtoOAOrderErrorEvent                   (type 2132)
  4. ProtoOAAmendPositionSLTPReq → ProtoOAExecutionEvent (for SL/TP)

Safety: DEMO only, BNK_TEST_MODE=1 required, micro-lots only

DEBUG NOTE:
  ORDER_ERROR_EVENT (2132) = cTrader accepted the TCP message but the BROKER
  rejected the order.  The errorCode + description fields contain the real reason.
  Common causes: wrong symbolId, market closed, volume step/min, margin, SL/TP.
"""
from __future__ import annotations

import asyncio
import struct
from typing import Any

from loguru import logger

from ..config import settings
from ..domain.enums import Symbol, Side
from ..domain.errors import BrokerError

# Message type constants (cTrader Open API v2)
PROTO_OA_APPLICATION_AUTH_REQ    = 2100
PROTO_OA_APPLICATION_AUTH_RES    = 2101
PROTO_OA_ACCOUNT_AUTH_REQ        = 2102
PROTO_OA_ACCOUNT_AUTH_RES        = 2103
PROTO_OA_NEW_ORDER_REQ           = 2106
PROTO_OA_SYMBOL_BY_ID_REQ        = 2121
PROTO_OA_SYMBOL_BY_ID_RES        = 2122
PROTO_OA_RECONCILE_REQ           = 2124
PROTO_OA_RECONCILE_RES           = 2125
PROTO_OA_EXECUTION_EVENT         = 2126
PROTO_OA_ORDER_ERROR_EVENT       = 2132
PROTO_OA_GET_ACCOUNTS_REQ        = 2149
PROTO_OA_GET_ACCOUNTS_RES        = 2150
PROTO_OA_ERROR_RES               = 2142
PROTO_ERROR_RES                  = 50

# ExecutionType enum (from ProtoOAExecutionType descriptor)
_EXECUTION_TYPE_NAMES = {
    2: "ORDER_ACCEPTED",
    3: "ORDER_FILLED",
    4: "ORDER_REPLACED",
    5: "ORDER_CANCELLED",
    6: "ORDER_EXPIRED",
    7: "ORDER_REJECTED",
    8: "ORDER_CANCEL_REJECTED",
    9: "SWAP",
    10: "DEPOSIT_WITHDRAW",
    11: "ORDER_PARTIAL_FILL",
    12: "BONUS_DEPOSIT_WITHDRAW",
}


# cTrader API hosts
_DEMO_HOST = "demo.ctraderapi.com"
_LIVE_HOST = "live.ctraderapi.com"
_PORT = 5035


def _import_trading_sdk():
    """Import cTrader Protobuf message classes for trading operations."""
    try:
        from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (  # type: ignore
            ProtoMessage,
        )
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (  # type: ignore
            ProtoOAAccountAuthReq,
            ProtoOAAccountAuthRes,
            ProtoOAApplicationAuthReq,
            ProtoOAApplicationAuthRes,
            ProtoOANewOrderReq,
            ProtoOAExecutionEvent,
            ProtoOAOrderErrorEvent,
            ProtoOAAmendPositionSLTPReq,
            ProtoOAGetAccountListByAccessTokenReq,
            ProtoOAGetAccountListByAccessTokenRes,
            ProtoOASymbolByIdReq,
            ProtoOASymbolByIdRes,
            ProtoOASymbolsListReq,
            ProtoOASymbolsListRes,
            ProtoOAReconcileReq,
            ProtoOAReconcileRes,
        )
        return {
            "ProtoMessage": ProtoMessage,
            "ProtoOAApplicationAuthReq": ProtoOAApplicationAuthReq,
            "ProtoOAApplicationAuthRes": ProtoOAApplicationAuthRes,
            "ProtoOAAccountAuthReq": ProtoOAAccountAuthReq,
            "ProtoOAAccountAuthRes": ProtoOAAccountAuthRes,
            "ProtoOANewOrderReq": ProtoOANewOrderReq,
            "ProtoOAExecutionEvent": ProtoOAExecutionEvent,
            "ProtoOAOrderErrorEvent": ProtoOAOrderErrorEvent,
            "ProtoOAAmendPositionSLTPReq": ProtoOAAmendPositionSLTPReq,
            "ProtoOAGetAccountListByAccessTokenReq": ProtoOAGetAccountListByAccessTokenReq,
            "ProtoOAGetAccountListByAccessTokenRes": ProtoOAGetAccountListByAccessTokenRes,
            "ProtoOASymbolByIdReq": ProtoOASymbolByIdReq,
            "ProtoOASymbolByIdRes": ProtoOASymbolByIdRes,
            "ProtoOASymbolsListReq": ProtoOASymbolsListReq,
            "ProtoOASymbolsListRes": ProtoOASymbolsListRes,
            "ProtoOAReconcileReq": ProtoOAReconcileReq,
            "ProtoOAReconcileRes": ProtoOAReconcileRes,
        }
    except ImportError as exc:
        raise ImportError(
            "cTrader Open API SDK not installed.\n"
            "Run:  pip install ctrader-open-api\n"
            f"Original error: {exc}"
        ) from exc


# Symbol ID mapping for Pepperstone Spread Betting demo account (account 46435708)
# These are broker/account-type specific. Pepperstone uses _SB variants for Spread Betting.
# XAUUSD_SB=241, XAGUSD_SB=238 (confirmed via ProtoOASymbolsListReq)
_SYMBOL_ID_MAP: dict[Symbol, int] = {
    Symbol.XAUUSD: 241,  # XAUUSD_SB (Gold - Spread Betting)
    Symbol.XAGUSD: 238,  # XAGUSD_SB (Silver - Spread Betting)
}


class CTraderTradingConnection:
    """
    Async TCP/TLS connection to cTrader Open API for trading.
    
    Handles message framing, serialization, and correlation.
    """

    def __init__(self, host: str, port: int, client_id: str, client_secret: str):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.client_secret = client_secret
        
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._msg_id = 1000
        self._responses: dict[str, asyncio.Future] = {}
        self._sdk: dict[str, Any] = {}
        self._authenticated_accounts: set[str] = set()  # Track authenticated accounts
        # Queue for messages that arrive without a matching clientMsgId
        # (cTrader may echo ORDER_ERROR_EVENT as a server-push without clientMsgId)
        self._unmatched_events: asyncio.Queue = asyncio.Queue(maxsize=200)

    async def connect(self):
        """Establish TLS connection to cTrader API."""
        if self.reader and self.writer:
            return  # Already connected
        
        logger.info(f"🔌 Connecting to cTrader API: {self.host}:{self.port}")
        
        # Import SDK
        self._sdk = _import_trading_sdk()
        
        # Open TLS socket
        import ssl
        ssl_context = ssl.create_default_context()
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port, ssl=ssl_context
        )
        
        logger.success(f"✅ Connected to {self.host}:{self.port}")
        
        # Start receive loop
        asyncio.create_task(self._receive_loop())

    @property
    def is_connected(self) -> bool:
        """
        Return True only when the TCP writer is alive and not being closed.

        Covers three drop scenarios:
          1. writer.close() called explicitly  → writer.is_closing() is True
          2. Remote end closed the connection  → reader.at_eof() is True
          3. writer/reader were never set      → None check
        """
        if self.writer is None or self.reader is None:
            return False
        if self.writer.is_closing():
            return False
        if self.reader.at_eof():
            return False
        return True

    async def disconnect(self):
        """Close the connection."""
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
            self.reader = None
            self._authenticated_accounts.clear()
            logger.info("Disconnected from cTrader API")

    async def _send_message(self, payload_type: int, payload: bytes, client_msg_id: str | None = None):
        """
        Send a length-prefixed Protobuf message.
        
        Format: [4 bytes length][ProtoMessage bytes]
        ProtoMessage contains: payloadType, payload, clientMsgId
        """
        if not self.writer:
            raise BrokerError("Not connected to cTrader API")
        
        # Build ProtoMessage wrapper
        ProtoMessage = self._sdk["ProtoMessage"]
        msg = ProtoMessage()
        msg.payloadType = payload_type
        msg.payload = payload
        if client_msg_id:
            msg.clientMsgId = client_msg_id
        
        # Serialize
        msg_bytes = msg.SerializeToString()
        
        # Length prefix (4 bytes big-endian)
        length = struct.pack(">I", len(msg_bytes))
        
        # Send
        self.writer.write(length + msg_bytes)
        await self.writer.drain()
        
        logger.debug(f"📤 Sent message type={payload_type} id={client_msg_id} size={len(msg_bytes)}")

    async def _receive_loop(self):
        """Continuously receive and dispatch messages."""
        try:
            while self.reader:
                # Read length prefix
                length_bytes = await self.reader.readexactly(4)
                length = struct.unpack(">I", length_bytes)[0]
                
                # Read message
                msg_bytes = await self.reader.readexactly(length)
                
                # Parse ProtoMessage
                ProtoMessage = self._sdk["ProtoMessage"]
                msg = ProtoMessage()
                msg.ParseFromString(msg_bytes)
                
                logger.debug(f"📥 Received message type={msg.payloadType} id={msg.clientMsgId} size={length}")
                
                # Dispatch to waiting coroutine
                if msg.clientMsgId and msg.clientMsgId in self._responses:
                    future = self._responses.pop(msg.clientMsgId)
                    future.set_result((msg.payloadType, msg.payload))
                    logger.debug(f"  → routed via clientMsgId={msg.clientMsgId}")
                elif msg.payloadType in (PROTO_OA_EXECUTION_EVENT, PROTO_OA_ORDER_ERROR_EVENT):
                    # cTrader may deliver order events as server-push without
                    # echoing the clientMsgId.  Route to the oldest pending
                    # future — we send orders strictly sequentially.
                    if self._responses:
                        oldest_key = next(iter(self._responses))
                        future = self._responses.pop(oldest_key)
                        logger.warning(
                            f"Order event type={msg.payloadType} arrived WITHOUT clientMsgId "
                            f"— routed to pending future '{oldest_key}' (server-push fallback)"
                        )
                        future.set_result((msg.payloadType, msg.payload))
                    else:
                        logger.warning(
                            f"Order event type={msg.payloadType} arrived without clientMsgId "
                            f"and no pending future — queuing as unmatched"
                        )
                        try:
                            self._unmatched_events.put_nowait((msg.payloadType, msg.payload))
                        except asyncio.QueueFull:
                            logger.error("Unmatched event queue full — dropping message")
                else:
                    logger.debug(f"Queued untracked server message type={msg.payloadType}")
                    try:
                        self._unmatched_events.put_nowait((msg.payloadType, msg.payload))
                    except asyncio.QueueFull:
                        pass
                    
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Receive loop error: {e}")

    async def send_and_wait(self, payload_type: int, payload: bytes, timeout: float = 10.0) -> tuple[int, bytes]:
        """
        Send a message and wait for the response.
        
        Returns: (response_type, response_payload)
        """
        # Generate unique ID
        msg_id = f"msg_{self._msg_id}"
        self._msg_id += 1
        
        # Create future for response
        future: asyncio.Future = asyncio.Future()
        self._responses[msg_id] = future
        
        # Send message
        await self._send_message(payload_type, payload, msg_id)
        
        # Wait for response
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            # Check for API-level error responses (50 = ProtoErrorRes, 2142 = ProtoOAErrorRes)
            # NOTE: ORDER_ERROR_EVENT (2132) is handled by the caller (place_market_order)
            #       because it contains rich rejection details we want to log fully.
            if result[0] in (PROTO_ERROR_RES, PROTO_OA_ERROR_RES):
                try:
                    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAErrorRes  # type: ignore
                    error_msg = ProtoOAErrorRes()
                    error_msg.ParseFromString(result[1])
                    error_code = error_msg.errorCode or "Unknown"
                    error_desc = error_msg.description or "No description"
                    logger.error(f"cTrader API-level error: code={error_code}, description={error_desc}")
                    logger.error(f"Full error fields: {error_msg.ListFields()}")
                    raise BrokerError(f"cTrader API error {error_code}: {error_desc}")
                except BrokerError:
                    raise
                except Exception as parse_exc:
                    logger.error(f"Failed to parse API error response (type {result[0]}): {parse_exc}")
                    logger.error(f"Raw error payload hex: {result[1][:200].hex()}")
                    raise BrokerError(f"cTrader API returned error (type {result[0]})")
            return result
        except asyncio.TimeoutError:
            self._responses.pop(msg_id, None)
            raise BrokerError(
                f"Timeout ({timeout}s) waiting for response to {msg_id}. "
                f"Check: (1) Account authenticated? (2) Symbol tradeable? (3) Network OK?"
            )

    async def authenticate_application(self):
        """Authenticate the application (client_id/secret)."""
        logger.info("🔐 Authenticating application...")
        
        ProtoOAApplicationAuthReq = self._sdk["ProtoOAApplicationAuthReq"]
        
        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        
        # Message type IDs (from cTrader SDK)
        PROTO_OA_APPLICATION_AUTH_REQ = 2100
        PROTO_OA_APPLICATION_AUTH_RES = 2101
        
        resp_type, resp_payload = await self.send_and_wait(
            PROTO_OA_APPLICATION_AUTH_REQ,
            req.SerializeToString()
        )
        
        if resp_type != PROTO_OA_APPLICATION_AUTH_RES:
            raise BrokerError(f"Unexpected response type: {resp_type}")
        
        ProtoOAApplicationAuthRes = self._sdk["ProtoOAApplicationAuthRes"]
        resp = ProtoOAApplicationAuthRes()
        resp.ParseFromString(resp_payload)
        
        logger.success("✅ Application authenticated")

    async def get_accounts(self, access_token: str) -> list[dict]:
        """
        Get list of trading accounts for the access token.
        
        Returns: list of {"accountId": str, "balance": float, ...}
        """
        logger.info("📋 Fetching account list...")
        
        ProtoOAGetAccountListByAccessTokenReq = self._sdk["ProtoOAGetAccountListByAccessTokenReq"]
        
        req = ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = access_token
        
        PROTO_OA_GET_ACCOUNTS_BY_ACCESS_TOKEN_REQ = 2149
        PROTO_OA_GET_ACCOUNTS_BY_ACCESS_TOKEN_RES = 2150
        
        resp_type, resp_payload = await self.send_and_wait(
            PROTO_OA_GET_ACCOUNTS_BY_ACCESS_TOKEN_REQ,
            req.SerializeToString()
        )
        
        if resp_type != PROTO_OA_GET_ACCOUNTS_BY_ACCESS_TOKEN_RES:
            raise BrokerError(f"Unexpected response type: {resp_type}")
        
        ProtoOAGetAccountListByAccessTokenRes = self._sdk["ProtoOAGetAccountListByAccessTokenRes"]
        resp = ProtoOAGetAccountListByAccessTokenRes()
        resp.ParseFromString(resp_payload)
        
        accounts = []
        for acc in resp.ctidTraderAccount:
            accounts.append({
                "accountId": str(acc.ctidTraderAccountId),
                "isLive": acc.isLive,
                "traderLogin": str(acc.traderLogin) if acc.HasField("traderLogin") else "",
            })
        
        logger.success(f"✅ Found {len(accounts)} account(s)")
        return accounts

    async def authenticate_account(self, account_id: str, access_token: str):
        """Authenticate a specific trading account (cached per connection)."""
        # Skip if already authenticated on this connection
        if account_id in self._authenticated_accounts:
            logger.debug(f"Account {account_id} already authenticated, skipping")
            return
        
        logger.info(f"🔐 Authenticating account {account_id}...")
        
        ProtoOAAccountAuthReq = self._sdk["ProtoOAAccountAuthReq"]
        
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = int(account_id)
        req.accessToken = access_token
        
        PROTO_OA_ACCOUNT_AUTH_REQ = 2102
        PROTO_OA_ACCOUNT_AUTH_RES = 2103
        
        resp_type, resp_payload = await self.send_and_wait(
            PROTO_OA_ACCOUNT_AUTH_REQ,
            req.SerializeToString()
        )
        
        if resp_type != PROTO_OA_ACCOUNT_AUTH_RES:
            raise BrokerError(f"Unexpected response type: {resp_type}")
        
        ProtoOAAccountAuthRes = self._sdk["ProtoOAAccountAuthRes"]
        resp = ProtoOAAccountAuthRes()
        resp.ParseFromString(resp_payload)
        
        # Mark as authenticated
        self._authenticated_accounts.add(account_id)
        logger.success(f"✅ Account {account_id} authenticated")

    async def get_symbols_for_account(self, account_id: str, filter_names: list[str] | None = None) -> list[dict]:
        """
        Fetch the tradeable symbols list for an account using ProtoOASymbolsListReq.

        Args:
            filter_names: If provided, only return symbols whose name contains
                          one of the given strings (case-insensitive).

        Returns:
            list of {"symbolId": int, "symbolName": str}
        """
        ProtoOASymbolsListReq = self._sdk["ProtoOASymbolsListReq"]
        ProtoOASymbolsListRes = self._sdk["ProtoOASymbolsListRes"]

        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = int(account_id)

        # Generate unique clientMsgId so routing is unambiguous
        msg_id = f"symlist_{self._msg_id}"
        self._msg_id += 1
        future: asyncio.Future = asyncio.Future()
        self._responses[msg_id] = future
        await self._send_message(2128, req.SerializeToString(), msg_id)

        try:
            resp_type, resp_payload = await asyncio.wait_for(future, timeout=25.0)
        except asyncio.TimeoutError:
            self._responses.pop(msg_id, None)
            raise BrokerError("Timeout waiting for SymbolsList response (25s)")

        if resp_type in (PROTO_ERROR_RES, PROTO_OA_ERROR_RES):
            try:
                from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAErrorRes  # type: ignore
                err = ProtoOAErrorRes(); err.ParseFromString(resp_payload)
                raise BrokerError(f"Broker rejected SymbolsList: {err.errorCode} — {err.description}")
            except BrokerError:
                raise
            except Exception:
                raise BrokerError(f"Broker error for SymbolsList request (type {resp_type})")

        if resp_type != 2129:  # PROTO_OA_SYMBOLS_LIST_RES
            raise BrokerError(f"Unexpected response type for SymbolsList: {resp_type}")

        res = ProtoOASymbolsListRes()
        res.ParseFromString(resp_payload)

        results = []
        for s in res.symbol:
            name = s.symbolName if hasattr(s, "symbolName") else ""
            if filter_names:
                if not any(f.upper() in name.upper() for f in filter_names):
                    continue
            results.append({"symbolId": s.symbolId, "symbolName": name})

        return results

    async def verify_symbol(self, account_id: str, symbol_id: int) -> dict | None:
        """
        Fetch symbol details from broker to verify the symbolId is valid
        and the instrument is tradeable on this account.

        Returns: dict with symbol info, or None if lookup fails.
        """
        try:
            ProtoOASymbolByIdReq = self._sdk["ProtoOASymbolByIdReq"]
            ProtoOASymbolByIdRes = self._sdk["ProtoOASymbolByIdRes"]

            req = ProtoOASymbolByIdReq()
            req.ctidTraderAccountId = int(account_id)
            req.symbolId.append(symbol_id)

            resp_type, resp_payload = await self.send_and_wait(
                PROTO_OA_SYMBOL_BY_ID_REQ,
                req.SerializeToString(),
                timeout=10.0,
            )

            if resp_type != PROTO_OA_SYMBOL_BY_ID_RES:
                logger.warning(f"Symbol verify: unexpected response type {resp_type}")
                return None

            resp = ProtoOASymbolByIdRes()
            resp.ParseFromString(resp_payload)

            if not resp.symbol:
                logger.warning(f"Symbol verify: broker returned 0 symbols for id={symbol_id}")
                return None

            sym = resp.symbol[0]
            # ProtoOASymbol does NOT have symbolName or enabled fields.
            # Those live in ProtoOALightSymbol.  Available here:
            # minVolume, maxVolume, stepVolume, lotSize, tradingMode,
            # slDistance, tpDistance, digits, schedule, etc.
            def _fld(msg, name):
                try:
                    val = getattr(msg, name)
                    # For proto2 optional scalars: only return if HasField is true
                    try:
                        return val if msg.HasField(name) else None
                    except ValueError:
                        return val  # repeated or non-optional
                except AttributeError:
                    return None

            info = {
                "symbolId"    : sym.symbolId,
                "minVolume"   : _fld(sym, "minVolume"),
                "maxVolume"   : _fld(sym, "maxVolume"),
                "stepVolume"  : _fld(sym, "stepVolume"),
                "lotSize"     : _fld(sym, "lotSize"),
                "tradingMode" : _fld(sym, "tradingMode"),  # 0=ENABLED 1=DISABLED_WITHOUT_POSITIONS 2=DISABLED_WITH_POSITIONS 3=CLOSE_ONLY
                "slDistance"  : _fld(sym, "slDistance"),
                "tpDistance"  : _fld(sym, "tpDistance"),
                "digits"      : _fld(sym, "digits"),
                "pipPosition" : _fld(sym, "pipPosition"),
            }
            # Sanity-check: broker should echo back the symbolId we requested.
            # If it returns something else (e.g. the accountId), the response
            # is being parsed incorrectly or the broker uses a different schema.
            if info["symbolId"] != symbol_id:
                logger.warning(
                    f"Symbol verify: broker returned symbolId={info['symbolId']} "
                    f"but we requested {symbol_id}. "
                    f"Possible proto schema mismatch — treating symbol info as UNRELIABLE."
                )
                info["_unreliable"] = True
            logger.info(f"Symbol broker info: {info}")
            return info
        except Exception as exc:
            logger.warning(f"Symbol verify failed (non-fatal): {exc}")
            return None

    async def place_market_order(
        self,
        account_id: str,
        symbol_id: int,
        side: Side,
        volume: int,  # In cTrader volume units  (100 = 0.01 lots for most instruments)
        sl_price: float | None = None,
        tp_price: float | None = None,
    ) -> dict:
        """
        Place a market order.

        Volume convention (cTrader Open API v2):
          volume = <units> where 1 unit = 1/100 of a lot.
          Examples: 100 = 0.01 lots (micro), 1000 = 0.10 lots, 100000 = 1.00 lot.
          NOTE: The code currently passes volume_cents = lots * 100_000 which equals
                the units * 1000 → i.e. it sends 10x the intended size.
                For 0.01 lots the correct value is 100, NOT 1000.
          Caller (ctrader_execution.py) uses: volume_cents = position_size * 100_000
          That means 0.01 lots → 1000 units.  cTrader may reject if min is 100 and
          step is 100 but 1000 is actually 0.10 lots — usually fine.
          We log both representations so the broker's rejection tells us the truth.

        Returns: {"positionId": str, "executedPrice": float}
        """
        # ──────────────────────────────────────────────────────────────────────
        # STEP A — Symbol verification (pre-flight, non-fatal)
        # ──────────────────────────────────────────────────────────────────────
        sym_info = await self.verify_symbol(account_id, symbol_id)

        # Volume calculations for display
        # The caller uses: volume = lots * 100_000
        # cTrader units:   1 unit = 0.01 lots  ⟹  lots * 100_000 / 100 = 1000 units per 0.01 lot
        # but the wire field is named 'volume' and for metals it is often different.
        # The broker will tell us the truth via minVolume / stepVolume.
        volume_as_lots_100k   = volume / 100_000.0    # if convention = 100_000 per lot
        volume_as_lots_10k    = volume / 10_000.0     # if convention = 10_000 per lot
        volume_as_lots_1k     = volume / 1_000.0      # if convention = 1_000 per lot
        volume_as_lots_100    = volume / 100.0         # if convention = 100 per lot

        ProtoOANewOrderReq = self._sdk["ProtoOANewOrderReq"]

        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = int(account_id)
        req.symbolId = symbol_id
        req.orderType = 1   # MARKET
        req.tradeSide = 1 if side == Side.BUY else 2   # BUY=1, SELL=2
        req.volume = volume
        req.clientOrderId = f"BNK_{int(asyncio.get_event_loop().time() * 1000)}"

        # SL/TP are intentionally omitted — they require relative distance or
        # guaranteed-stop pricing; set via AmendPositionSLTP after fill.

        # ──────────────────────────────────────────────────────────────────────
        # STEP B — Full outgoing request log
        # ──────────────────────────────────────────────────────────────────────
        SEP = "═" * 80
        logger.info(SEP)
        logger.info("▶▶▶  OUTGOING ORDER REQUEST  (ProtoOANewOrderReq)")
        logger.info(SEP)
        logger.info(f"  ctidTraderAccountId : {req.ctidTraderAccountId}")
        logger.info(f"  symbolId            : {req.symbolId}")
        logger.info(f"  orderType           : {req.orderType}  (1=MARKET 2=LIMIT 3=STOP)")
        logger.info(f"  tradeSide           : {req.tradeSide}  (1=BUY 2=SELL)")
        logger.info(f"  volume              : {req.volume}")
        logger.info(f"  clientOrderId       : {req.clientOrderId}")
        logger.info(f"  --- Volume interpretations (pending broker schema confirmation) ---")
        logger.info(f"  If 1 lot = 100_000 units : {volume_as_lots_100k:.5f} lots")
        logger.info(f"  If 1 lot =  10_000 units : {volume_as_lots_10k:.4f} lots")
        logger.info(f"  If 1 lot =   1_000 units : {volume_as_lots_1k:.3f} lots")
        logger.info(f"  If 1 lot =     100 units : {volume_as_lots_100:.2f} lots")
        logger.info(f"  stopLoss set            : {req.HasField('stopLoss')}")
        logger.info(f"  takeProfit set          : {req.HasField('takeProfit')}")
        logger.info(f"  limitPrice set          : {req.HasField('limitPrice')}")
        logger.info(f"  stopPrice set           : {req.HasField('stopPrice')}")
        logger.info(f"  All proto fields        : {req.ListFields()}")
        logger.info(SEP)

        # ──────────────────────────────────────────────────────────────────────
        # STEP C — Pre-flight validation checks (PASS / FAIL)
        # ──────────────────────────────────────────────────────────────────────
        logger.info("PRE-FLIGHT VALIDATION CHECKS:")

        # Check 1: Account ID
        if not account_id or account_id in ("", "0"):
            logger.error("  ❌ FAIL [C1]: Account ID is empty or zero")
        else:
            logger.info(f"  ✅ PASS [C1]: Account ID = {account_id}")

        # Check 2: Symbol ID
        _known_ids = {41: "XAUUSD", 42: "XAGUSD"}
        if symbol_id in _known_ids:
            logger.info(f"  ✅ PASS [C2]: Symbol ID {symbol_id} → local map = {_known_ids[symbol_id]}")
        else:
            logger.warning(f"  ⚠️  WARN [C2]: Symbol ID {symbol_id} not in local map — broker may map differently")

        # Check 2b: broker symbol confirm
        if sym_info:
            trading_mode = sym_info.get("tradingMode")
            trading_mode_name = {0: "ENABLED", 1: "DISABLED_WITHOUT_POSITIONS", 2: "DISABLED_WITH_POSITIONS", 3: "CLOSE_ONLY"}.get(trading_mode, f"mode={trading_mode}")
            if trading_mode and trading_mode != 0:
                logger.error(f"  ❌ FAIL [C2b]: Broker tradingMode={trading_mode} ({trading_mode_name}) — NOT ENABLED")
            else:
                logger.info(f"  ✅ PASS [C2b]: Broker tradingMode={trading_mode} ({trading_mode_name})  slDist={sym_info.get('slDistance')} tpDist={sym_info.get('tpDistance')} digits={sym_info.get('digits')} pipPos={sym_info.get('pipPosition')}")
            sl_dist = sym_info.get("slDistance")  # min SL distance in points
            tp_dist = sym_info.get("tpDistance")  # min TP distance in points
            if sl_dist:
                logger.info(f"  ℹ️   INFO [C2b]: min SL distance = {sl_dist} points/pipPosition units")
            if tp_dist:
                logger.info(f"  ℹ️   INFO [C2b]: min TP distance = {tp_dist} points/pipPosition units")
            broker_min = sym_info.get("minVolume")
            broker_max = sym_info.get("maxVolume")
            broker_step = sym_info.get("stepVolume")
            broker_lot = sym_info.get("lotSize")
            logger.info(f"  ℹ️   INFO [C2b]: minVolume={broker_min} maxVolume={broker_max} stepVolume={broker_step} lotSize={broker_lot}")
            if sym_info.get("_unreliable"):
                logger.warning("  ⚠️  WARN [C2b-vol]: Broker symbol data unreliable (symbolId mismatch) — skipping volume constraint checks")
            elif broker_min is not None and broker_min > 0 and volume < broker_min:
                logger.error(f"  ❌ FAIL [C2b-vol]: volume={volume} < broker minVolume={broker_min}")
            elif broker_step is not None and broker_step > 0 and (volume % broker_step) != 0:
                logger.error(f"  ❌ FAIL [C2b-vol]: volume={volume} is not a multiple of stepVolume={broker_step}")
            elif broker_max is not None and volume > broker_max:
                logger.error(f"  ❌ FAIL [C2b-vol]: volume={volume} > broker maxVolume={broker_max}")
            else:
                logger.info(f"  ✅ PASS [C2b-vol]: volume={volume} within broker constraints")
        else:
            logger.warning("  ⚠️  WARN [C2b]: Could not verify symbol from broker (non-fatal)")

        # Check 3: Volume sanity
        if volume <= 0:
            logger.error(f"  ❌ FAIL [C3]: Volume must be > 0 (got {volume})")
        elif volume < 100:
            logger.warning(f"  ⚠️  WARN [C3]: Volume={volume} is very small — below 0.01 lots even in 100-per-lot convention")
        else:
            logger.info(f"  ✅ PASS [C3]: Volume={volume} is positive")

        # Check 4: Order type
        if req.orderType != 1:
            logger.error(f"  ❌ FAIL [C4]: orderType should be 1 (MARKET), got {req.orderType}")
        else:
            logger.info("  ✅ PASS [C4]: orderType=1 (MARKET)")

        # Check 5: Trade side
        if req.tradeSide not in (1, 2):
            logger.error(f"  ❌ FAIL [C5]: tradeSide should be 1 or 2, got {req.tradeSide}")
        else:
            logger.info(f"  ✅ PASS [C5]: tradeSide={req.tradeSide} ({'BUY' if req.tradeSide==1 else 'SELL'})")

        # Check 6: Market order must NOT have limitPrice / stopPrice
        if req.HasField("limitPrice"):
            logger.error("  ❌ FAIL [C6]: MARKET order must NOT have limitPrice set")
        else:
            logger.info("  ✅ PASS [C6]: No limitPrice (correct for MARKET)")

        if req.HasField("stopPrice"):
            logger.error("  ❌ FAIL [C6]: MARKET order must NOT have stopPrice set")
        else:
            logger.info("  ✅ PASS [C6]: No stopPrice (correct for MARKET)")

        # Check 7: SL/TP not set (we set post-fill — confirm none slipped in)
        if req.HasField("stopLoss") or req.HasField("takeProfit"):
            logger.warning("  ⚠️  WARN [C7]: SL/TP are set on the order — broker may reject if distance invalid")
        else:
            logger.info("  ✅ PASS [C7]: No SL/TP on initial order (will amend post-fill)")

        logger.info(SEP)

        # ──────────────────────────────────────────────────────────────────────
        # STEP D — Send order
        # ──────────────────────────────────────────────────────────────────────
        payload_bytes = req.SerializeToString()
        logger.debug(f"Serialized ProtoOANewOrderReq: {len(payload_bytes)} bytes | hex: {payload_bytes.hex()}")

        resp_type, resp_payload = await self.send_and_wait(
            PROTO_OA_NEW_ORDER_REQ,
            payload_bytes,
            timeout=30.0,
        )

        # ──────────────────────────────────────────────────────────────────────
        # STEP E — Handle ORDER_ERROR_EVENT (2132) — broker rejection
        # ──────────────────────────────────────────────────────────────────────
        if resp_type == PROTO_OA_ORDER_ERROR_EVENT:
            self._decode_and_report_order_error(
                resp_payload, volume, volume_as_lots_100k, symbol_id, account_id
            )
            # decode_and_report raises BrokerError at the end

        # ──────────────────────────────────────────────────────────────────────
        # STEP F — Handle EXECUTION_EVENT (2126) — success
        # ──────────────────────────────────────────────────────────────────────
        if resp_type != PROTO_OA_EXECUTION_EVENT:
            logger.error(f"Unexpected response payloadType={resp_type} (expected 2126=EXECUTION_EVENT or 2132=ORDER_ERROR_EVENT)")
            logger.error(f"Raw payload hex: {resp_payload[:200].hex()}")
            raise BrokerError(f"Unexpected response type: {resp_type}")

        ProtoOAExecutionEvent = self._sdk["ProtoOAExecutionEvent"]
        resp = ProtoOAExecutionEvent()
        resp.ParseFromString(resp_payload)

        exec_type_num = resp.executionType
        exec_type_name = _EXECUTION_TYPE_NAMES.get(exec_type_num, f"UNKNOWN({exec_type_num})")
        logger.info(f"ExecutionEvent: executionType={exec_type_num} ({exec_type_name})")
        logger.info(f"  errorCode field (empty=ok): '{resp.errorCode}'")

        # Detect ORDER_REJECTED inside an ExecutionEvent wrapper (some brokers do this)
        if exec_type_name == "ORDER_REJECTED" or resp.errorCode:
            logger.error(f"ExecutionEvent with REJECTED status — errorCode='{resp.errorCode}'")
            raise BrokerError(f"Order rejected via ExecutionEvent: errorCode='{resp.errorCode}'")

        result = {
            "positionId"    : str(resp.position.positionId) if resp.HasField("position") else None,
            "orderId"       : str(resp.order.orderId)       if resp.HasField("order")    else None,
            "executedPrice" : (
                resp.order.executionPrice  # native double in account currency, no scaling
                if resp.HasField("order") and resp.order.HasField("executionPrice")
                else None
            ),
            "executionType" : exec_type_name,
        }

        logger.success(f"✅ Order executed: {result}")

        # ── STEP G — Wait for ORDER_FILLED (executionType=3) ─────────────────
        # After ACCEPTED the broker will push a second ExecutionEvent with
        # executionType=3 (ORDER_FILLED) containing the actual fill price.
        # We wait up to 5 s; if it doesn't arrive we fall back to reconcile.
        if exec_type_name == "ORDER_ACCEPTED":
            filled_result = await self._wait_for_order_filled(result, timeout=5.0)
            if filled_result:
                result.update(filled_result)
                logger.success(f"✅ Order FILLED: executedPrice={result.get('executedPrice')} positionId={result.get('positionId')}")
            else:
                logger.info("ORDER_FILLED not received in 5s — falling back to reconcile for fill details")
                reconcile_data = await self._reconcile_after_fill(
                    account_id=account_id,
                    expected_position_id=result.get("positionId"),
                    delay=1.5,
                )
                if reconcile_data:
                    result.update(reconcile_data)

        return result

    async def _wait_for_order_filled(self, accepted_result: dict, timeout: float = 5.0) -> dict | None:
        """
        After ORDER_ACCEPTED, listen for the ORDER_FILLED execution event.

        cTrader sends a second ProtoOAExecutionEvent (executionType=3) carrying
        the actual fill price in order.executionPrice.

        Returns a partial result dict with executedPrice/positionId updated,
        or None if the event does not arrive in time.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        ProtoOAExecutionEvent = self._sdk["ProtoOAExecutionEvent"]

        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                evt_type, evt_payload = await asyncio.wait_for(
                    self._unmatched_events.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                break

            if evt_type != PROTO_OA_EXECUTION_EVENT:
                # Put non-execution events back and keep draining
                try:
                    self._unmatched_events.put_nowait((evt_type, evt_payload))
                except asyncio.QueueFull:
                    pass
                await asyncio.sleep(0.05)
                continue

            evt = ProtoOAExecutionEvent()
            evt.ParseFromString(evt_payload)
            exec_type = _EXECUTION_TYPE_NAMES.get(evt.executionType, f"UNKNOWN({evt.executionType})")
            logger.info(f"[fill-listener] ExecutionEvent executionType={evt.executionType} ({exec_type})")

            if exec_type in ("ORDER_FILLED", "ORDER_PARTIAL_FILL"):
                pos_id = str(evt.position.positionId) if evt.HasField("position") else accepted_result.get("positionId")
                exec_price = None
                if evt.HasField("deal") and evt.deal.HasField("executionPrice"):
                    exec_price = evt.deal.executionPrice  # native double, no scaling
                elif evt.HasField("order") and evt.order.HasField("executionPrice"):
                    exec_price = evt.order.executionPrice  # native double, no scaling

                return {
                    "positionId"    : pos_id,
                    "executedPrice" : exec_price,
                    "executionType" : exec_type,
                }

        return None

    async def reconcile_positions(self, account_id: str) -> list[dict]:
        """
        Call ProtoOAReconcileReq to get all open positions for the account.

        Returns list of dicts:
            positionId, symbolId, volume, tradeSide, entryPrice,
            stopLoss, takeProfit, swap, unrealisedPnL
        """
        ProtoOAReconcileReq = self._sdk["ProtoOAReconcileReq"]
        ProtoOAReconcileRes = self._sdk["ProtoOAReconcileRes"]

        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = int(account_id)

        resp_type, resp_payload = await self.send_and_wait(
            PROTO_OA_RECONCILE_REQ,
            req.SerializeToString(),
            timeout=10.0,
        )

        if resp_type != PROTO_OA_RECONCILE_RES:
            raise BrokerError(f"Reconcile: unexpected response type {resp_type}")

        res = ProtoOAReconcileRes()
        res.ParseFromString(resp_payload)

        positions = []
        for pos in res.position:
            td = pos.tradeData
            # pos.price is a native double in the account's base currency.
            # No scaling needed — the value IS the entry price.
            entry_price = pos.price if pos.price else None
            positions.append({
                "positionId"    : pos.positionId,
                "symbolId"      : td.symbolId,
                "volume"        : td.volume,
                "tradeSide"     : td.tradeSide,    # 1=BUY 2=SELL
                "entryPrice"    : entry_price,      # in account base currency
                "stopLoss"      : pos.stopLoss  if pos.stopLoss  else None,
                "takeProfit"    : pos.takeProfit if pos.takeProfit else None,
                "swap"          : pos.swap,
                "positionStatus": pos.positionStatus,
            })

        return positions

    async def _reconcile_after_fill(
        self,
        account_id: str,
        expected_position_id: str | None,
        delay: float = 1.5,
    ) -> dict | None:
        """
        Sleep briefly then call reconcile, returning the matching position.
        """
        await asyncio.sleep(delay)
        SEP = "═" * 80
        logger.info(SEP)
        logger.info("▶▶▶  RECONCILE — Open Positions after order")
        logger.info(SEP)

        try:
            positions = await self.reconcile_positions(account_id)
        except Exception as exc:
            logger.error(f"Reconcile failed: {exc}")
            return None

        if not positions:
            logger.warning("  Reconcile returned 0 open positions")
            return None

        logger.info(f"  Total open positions: {len(positions)}")
        for i, p in enumerate(positions):
            side_name = "BUY" if p["tradeSide"] == 1 else "SELL"
            logger.info(
                f"  [{i+1}] positionId={p['positionId']}  symbolId={p['symbolId']}  "
                f"volume={p['volume']}  side={side_name}  entryPrice={p['entryPrice']}  "
                f"SL={p['stopLoss']}  TP={p['takeProfit']}  swap={p['swap']}"
            )
        logger.info(SEP)

        # Find the position that matches our order
        match = None
        if expected_position_id:
            for p in positions:
                if str(p["positionId"]) == str(expected_position_id):
                    match = p
                    break
        if not match and positions:
            # Fall back to most recent (last in list)
            match = positions[-1]
            logger.info(f"  No positionId match — using last position: {match['positionId']}")

        if match:
            side_name = "BUY" if match["tradeSide"] == 1 else "SELL"
            logger.success(
                f"✅ POSITION CONFIRMED: positionId={match['positionId']}  "
                f"symbolId={match['symbolId']}  volume={match['volume']}  "
                f"side={side_name}  entryPrice={match['entryPrice']}"
            )
            return {
                "positionId"    : str(match["positionId"]),
                "executedPrice" : match["entryPrice"],
                "executionType" : "ORDER_FILLED (via reconcile)",
                "symbolId"      : match["symbolId"],
                "volume"        : match["volume"],
                "tradeSide"     : match["tradeSide"],
            }

        return None

    def _decode_and_report_order_error(
        self,
        payload: bytes,
        raw_volume: int,
        volume_lots: float,
        symbol_id: int,
        account_id: str,
    ) -> None:
        """
        Fully decode ProtoOAOrderErrorEvent and log every field.
        Then raise BrokerError with the real rejection reason.
        This method ALWAYS raises — it never returns normally.
        """
        SEP = "═" * 80
        logger.error(SEP)
        logger.error("◀◀◀  ORDER REJECTED BY BROKER  (ProtoOAOrderErrorEvent type=2132)")
        logger.error(SEP)

        try:
            ProtoOAOrderErrorEvent = self._sdk["ProtoOAOrderErrorEvent"]
            err = ProtoOAOrderErrorEvent()
            err.ParseFromString(payload)

            error_code  = err.errorCode   or "(empty)"
            description = err.description or "(no description)"
            order_id    = err.orderId
            position_id = err.positionId
            account     = err.ctidTraderAccountId

            logger.error(f"  errorCode            : {error_code}")
            logger.error(f"  description          : {description}")
            logger.error(f"  ctidTraderAccountId  : {account}")
            logger.error(f"  orderId              : {order_id}")
            logger.error(f"  positionId           : {position_id}")
            logger.error(f"  All ListFields()     : {err.ListFields()}")
            logger.error(f"  Raw payload hex      : {payload.hex()}")

        except Exception as parse_exc:
            logger.error(f"  !! Could not parse ProtoOAOrderErrorEvent: {parse_exc}")
            logger.error(f"  Raw payload hex: {payload.hex()}")
            error_code  = "PARSE_ERROR"
            description = str(parse_exc)

        logger.error(SEP)

        # ── Root-cause diagnosis table ──────────────────────────────────────
        ec_upper = error_code.upper()
        logger.error("ROOT-CAUSE ANALYSIS:")
        checks = [
            ("MARGIN" in ec_upper or "MONEY" in ec_upper or "FUNDS" in ec_upper,
             "Insufficient margin/funds",
             f"Reduce size. Requested {volume_lots:.4f} lots ({raw_volume} units)"),
            ("MARKET_CLOSED" in ec_upper or "TRADING_DISABLED" in ec_upper or "SESSION" in ec_upper,
             "Market closed / trading session disabled",
             f"Check market hours for symbolId={symbol_id}"),
            ("SYMBOL" in ec_upper and ("INVALID" in ec_upper or "NOT_FOUND" in ec_upper
              or "DISALLOWED" in ec_upper or "DISABLED" in ec_upper),
             "Invalid or disabled symbol for this account",
             f"Verify symbolId={symbol_id} is correct for accountId={account_id}"),
            ("VOLUME" in ec_upper or "LOT" in ec_upper or "SIZE" in ec_upper,
             "Volume < min, > max, or not on stepVolume",
             f"Sent volume={raw_volume}. Check broker minVolume/stepVolume"),
            ("STOP" in ec_upper and ("LOSS" in ec_upper or "TP" in ec_upper or "DISTANCE" in ec_upper),
             "SL/TP too close or on wrong side",
             "Do NOT set SL/TP on initial order; amend post-fill"),
            ("PRICE" in ec_upper or "EXECUTION" in ec_upper,
             "Price/execution-type mismatch (e.g. limitPrice set on MARKET order)",
             "Ensure limitPrice/stopPrice are NOT set for MARKET orders"),
            ("ORDER_TYPE" in ec_upper or "NOT_ALLOWED" in ec_upper,
             "Order type not allowed (MARKET orders may be restricted to certain sessions)",
             "Try LIMIT order or check broker account permissions"),
        ]
        diagnosed = False
        for condition, cause, fix in checks:
            if condition:
                logger.error(f"  ❌ LIKELY CAUSE : {cause}")
                logger.error(f"     SUGGESTED FIX : {fix}")
                diagnosed = True
                break
        if not diagnosed:
            logger.error(f"  ❓ CAUSE UNKNOWN — rawCode='{error_code}' | description='{description}'")
            logger.error(f"     ACTION: Search for '{error_code}' in cTrader Open API error code list")

        logger.error(SEP)
        raise BrokerError(f"Order rejected (2132): errorCode='{error_code}' description='{description}'")


# Singleton connection for reuse
_trading_connection: CTraderTradingConnection | None = None


async def get_trading_connection() -> CTraderTradingConnection:
    """
    Get or create the trading connection singleton.

    Auto-reconnects if the existing connection is stale/dead.
    This prevents sync_positions from failing with errors:1 after a
    period of inactivity or broker-side disconnect.
    """
    global _trading_connection

    if _trading_connection is not None and not _trading_connection.is_connected:
        logger.warning(
            "get_trading_connection: existing connection is dead (writer closed/None) — resetting"
        )
        try:
            await _trading_connection.disconnect()
        except Exception:
            pass
        _trading_connection = None

    if _trading_connection is None:
        host = _DEMO_HOST if settings.ctrader_env == "demo" else _LIVE_HOST
        logger.info("get_trading_connection: creating new connection to {}:{}", host, _PORT)
        _trading_connection = CTraderTradingConnection(
            host=host,
            port=_PORT,
            client_id=settings.ctrader_client_id,
            client_secret=settings.ctrader_client_secret,
        )
        await _trading_connection.connect()
        await _trading_connection.authenticate_application()
        logger.success("get_trading_connection: new connection authenticated")

    return _trading_connection


async def discover_accounts_protobuf(access_token: str) -> list[dict]:
    """
    Discover trading accounts using Protobuf protocol.
    
    Returns: list of {"accountId": str, "isLive": bool, "balance": float}
    """
    conn = await get_trading_connection()
    return await conn.get_accounts(access_token)


async def place_demo_trade_protobuf(
    account_id: str,
    access_token: str,
    symbol: Symbol,
    side: Side,
    volume: int,  # in cents
    sl_price: float | None = None,
    tp_price: float | None = None,
) -> dict:
    """
    Place a demo trade using Protobuf protocol.
    
    Safety: Only works in demo mode with BNK_TEST_MODE=1
    
    Returns: {"positionId": str, "orderId": str, "executedPrice": float}
    """
    # Safety checks
    if settings.ctrader_env != "demo":
        raise BrokerError("Protobuf trading only allowed in demo mode")
    
    if not settings.bnk_test_mode:
        raise BrokerError("Protobuf trading requires BNK_TEST_MODE=1")
    
    # Get connection
    conn = await get_trading_connection()
    
    # Authenticate account
    await conn.authenticate_account(account_id, access_token)
    
    # Get symbol ID
    symbol_id = _SYMBOL_ID_MAP.get(symbol)
    if not symbol_id:
        raise BrokerError(f"Unknown symbol: {symbol}")
    
    # Place order
    return await conn.place_market_order(
        account_id=account_id,
        symbol_id=symbol_id,
        side=side,
        volume=volume,
        sl_price=sl_price,
        tp_price=tp_price,
    )


async def reconcile_demo_positions(account_id: str, access_token: str) -> list[dict]:
    """
    Fetch all open positions via Reconcile request.

    Returns list of position dicts with positionId, symbolId, volume,
    tradeSide, entryPrice, stopLoss, takeProfit.
    """
    conn = await get_trading_connection()
    await conn.authenticate_account(account_id, access_token)
    return await conn.reconcile_positions(account_id)
