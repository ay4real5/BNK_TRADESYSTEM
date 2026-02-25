"""
cTrader Demo Execution Service

Handles real trade execution via cTrader Open API (demo environment only).

Flow:
1. Receive approved signal
2. Pre-trade safeguards (risk, spread, SL validation)
3. Calculate position size using risk manager
4. Send market order via cTrader API
5. Attach SL and TP immediately
6. Store remote position ID
7. Persist in database

Position Sync:
- Poll open positions from cTrader
- Sync to local DB
- Handle remote closures, slippage, partial fills
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import httpx
from loguru import logger

from ..config import settings
from ..data import market_data, storage
from ..domain.enums import LockReason, Mode, Side, SignalStatus, Symbol, TradeOutcome
from ..domain.errors import BrokerError, LockError, RiskViolation
from ..domain.models import TradeIdea, TradeResult
from ..execution.safeguards import check_sl_tp_valid, check_spread
from ..services import account_manager, locks, risk_manager
from ..services.ctrader_oauth import oauth_service
from ..integration.ctrader_trading import place_demo_trade_protobuf
from .base import Executor


DB_PATH = "data/trading.db"


class CTraderExecutionService(Executor):
    """
    cTrader execution service with pre-trade safeguards and position sync.
    
    Only operates in DEMO mode. Enforces risk limits before every trade.
    """

    def __init__(self) -> None:
        # Protobuf trading via TCP/TLS (not HTTP REST)
        logger.info("CTraderExecutionService initialized (env: {}) - using Protobuf protocol", settings.ctrader_env)

    async def _get_headers(self) -> dict[str, str]:
        """Get authorization headers with valid access token."""
        token = await oauth_service.get_valid_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def open_trade(self, idea: TradeIdea, bypass_risk: bool = False) -> TradeResult:
        """
        Execute trade with full safeguards.
        
        Pre-trade checks:
        - Risk engine approval
        - Spread validation
        - SL/TP geometry validation
        - Account margin check
        
        Args:
            bypass_risk: Skip risk/lock checks (admin/force-execute path only)
        
        Returns:
            TradeResult with remote position ID
        """
        # Step 1: Risk approval (skipped when bypass_risk=True)
        if not bypass_risk:
            can_trade, reject_reason = await self._check_risk_approval(idea)
            if not can_trade:
                raise RiskViolation(f"Risk check failed: {reject_reason}")

        # Step 2: Safeguards
        await self._run_safeguards(idea)

        # Step 3: Calculate position size
        position_size = await self._calculate_position_size(idea)

        # Step 4: Get current market price (used as fill-price fallback only;
        # the broker executes at its own market price regardless)
        try:
            current_price = await market_data.fetch_price(idea.symbol)
        except Exception:
            logger.warning(
                "Market data unavailable for {} — using signal entry {} as fill-price fallback",
                idea.symbol.value,
                idea.entry,
            )
            current_price = idea.entry

        # Step 5: Get access token
        access_token = await oauth_service.get_valid_access_token()
        account_id = settings.ctrader_account_id

        # Step 6: Calculate volume in cents (cTrader uses cents, 100000 = 1 lot)
        volume_cents = int(position_size * 100000)

        # Log order before sending
        logger.info(
            "📤 Submitting cTrader order via Protobuf: {} {} {} lots @ market | SL: {} | TP: {}",
            idea.side.value.upper(),
            idea.symbol.value,
            position_size,
            idea.sl,
            idea.tp,
        )
        logger.debug(f"Volume in cents: {volume_cents}, Account: {account_id}")

        # Step 7: Send market order via Protobuf — record latency
        _order_send_ts = time.monotonic()
        try:
            order_result = await place_demo_trade_protobuf(
                account_id=account_id,
                access_token=access_token,
                symbol=idea.symbol,
                side=idea.side,
                volume=volume_cents,
                sl_price=idea.sl,
                tp_price=idea.tp,
            )
        except Exception as exc:
            logger.error("❌ Order submission failed: {}", exc)
            raise BrokerError(f"Order submission failed: {exc}")
        _exec_latency_ms = int((time.monotonic() - _order_send_ts) * 1000)

        # Step 8: Extract position ID and execution price
        position_id = str(order_result.get("positionId") or order_result.get("orderId") or "")
        order_id = str(order_result.get("orderId") or order_result.get("positionId") or "")
        fill_price = order_result.get("executedPrice") or current_price

        if not position_id:
            raise BrokerError("No position ID returned from broker")

        # Step 8b: Compute entry slippage (positive = worse fill for us)
        if idea.side == Side.BUY:
            _entry_slippage = round(fill_price - idea.entry, 5)
        else:
            _entry_slippage = round(idea.entry - fill_price, 5)

        # Step 8c: Look up spread from latest tick (fallback to asset default)
        _spread_at_entry = await self._get_latest_spread(idea.symbol)

        logger.success(
            "✅ cTrader position opened: ID={} | Fill={} | Signal={} | Slippage={:+.3f} | Latency={}ms",
            position_id,
            fill_price,
            idea.entry,
            _entry_slippage,
            _exec_latency_ms,
        )

        # Step 9: Create local position record
        # Use settings.mode (the runtime mode: demo/live) rather than idea.mode,
        # because signals are generated with mode=paper but executed via real broker.
        trade = TradeResult(
            signal_id=idea.id,
            ts_open=datetime.now(timezone.utc),
            symbol=idea.symbol,
            side=idea.side,
            entry=fill_price,
            sl=idea.sl,
            tp=idea.tp,
            size=position_size,
            outcome=TradeOutcome.OPEN,
            pnl=0.0,
            mode=settings.mode,
            max_adverse_excursion=0.0,
            broker_position_id=position_id,
            broker_order_id=order_id,
            execution_latency_ms=_exec_latency_ms,
            entry_slippage=_entry_slippage,
            spread_at_entry=_spread_at_entry,
        )

        # Step 10: Store position in database with remote ID
        trade_id = await storage.save_trade(trade)
        # Also keep legacy secrets-table mapping for sync compatibility
        await self._store_remote_position_id(trade_id, position_id)

        # Step 11: Update signal status
        if idea.id:
            await storage.update_signal_status(idea.id, SignalStatus.EXECUTED)

        logger.info(
            "Position #{} opened in DB with remote ID {}",
            trade_id,
            position_id,
        )

        # Audit log
        import json as _json
        await storage.log_execution_event(
            "order_placed",
            trade_id=trade_id,
            symbol=idea.symbol.value,
            detail=_json.dumps({
                "side": idea.side.value,
                "entry": fill_price,
                "sl": idea.sl,
                "tp": idea.tp,
                "size": position_size,
                "slippage": _entry_slippage,
                "latency_ms": _exec_latency_ms,
                "remote_id": position_id,
            }),
        )

        trade.id = trade_id
        return trade

    async def close_trade(self, trade: TradeResult, current_price: float) -> TradeResult:
        """
        Close position via cTrader API.
        
        Args:
            trade: Local trade record
            current_price: Current market price
        
        Returns:
            Updated trade with final PnL
        """
        # Get remote position ID
        remote_id = await self._get_remote_position_id(trade.id)
        if not remote_id:
            raise BrokerError(f"No remote position ID for trade {trade.id}")

        logger.info("Closing cTrader position {} (local ID: {})", remote_id, trade.id)

        # Send close request
        try:
            close_result = await self._send_close_order(remote_id)
        except Exception as exc:
            logger.error("Failed to close position {}: {}", remote_id, exc)
            raise BrokerError(f"Close order failed: {exc}")

        # Update trade record
        trade.ts_close = datetime.now(timezone.utc)
        trade.outcome = TradeOutcome.WIN if trade.pnl > 0 else TradeOutcome.LOSS

        logger.success(
            "✅ Position {} closed | PnL: ${:.2f} | Outcome: {}",
            remote_id,
            trade.pnl,
            trade.outcome.value,
        )

        await storage.update_trade(trade)
        return trade

    async def update_trade(self, trade: TradeResult, current_price: float) -> TradeResult:
        """
        Mark-to-market an open trade.
        
        Updates PnL and MAE based on current price.
        Checks if SL or TP has been hit.
        """
        # Calculate current PnL
        if trade.side == Side.BUY:
            pnl = (current_price - trade.entry) * trade.size
        else:
            pnl = (trade.entry - current_price) * trade.size

        trade.pnl = round(pnl, 2)

        # Update MAE (worst drawdown)
        if pnl < trade.max_adverse_excursion:
            trade.max_adverse_excursion = pnl

        # Check if SL or TP hit
        if trade.side == Side.BUY:
            if current_price <= trade.sl:
                trade.outcome = TradeOutcome.LOSS
                trade.pnl = round((trade.sl - trade.entry) * trade.size, 2)
                trade.ts_close = datetime.now(timezone.utc)
            elif current_price >= trade.tp:
                trade.outcome = TradeOutcome.WIN
                trade.pnl = round((trade.tp - trade.entry) * trade.size, 2)
                trade.ts_close = datetime.now(timezone.utc)
        else:  # SELL
            if current_price >= trade.sl:
                trade.outcome = TradeOutcome.LOSS
                trade.pnl = round((trade.entry - trade.sl) * trade.size, 2)
                trade.ts_close = datetime.now(timezone.utc)
            elif current_price <= trade.tp:
                trade.outcome = TradeOutcome.WIN
                trade.pnl = round((trade.entry - trade.tp) * trade.size, 2)
                trade.ts_close = datetime.now(timezone.utc)

        return trade

    # -------------------------------------------------------------------------
    # Pre-trade safeguards
    # -------------------------------------------------------------------------

    async def _check_risk_approval(self, idea: TradeIdea) -> tuple[bool, str | None]:
        """
        Validate signal against risk engine.
        
        Returns:
            (can_trade: bool, reject_reason: str | None)
        """
        try:
            # Check risk state
            state = await locks.check_can_trade()
            return True, None
        except LockError as exc:
            return False, str(exc)

    async def _run_safeguards(self, idea: TradeIdea) -> None:
        """
        Run all pre-trade safeguards.
        
        Raises:
            RiskViolation: If any check fails
        """
        # Check SL/TP geometry
        check_sl_tp_valid(idea)

        # Check spread
        try:
            current_price = await market_data.fetch_price(idea.symbol)
            # For demo, assume tight spread (0.30 for XAUUSD, 0.03 for XAGUSD)
            spread = 0.30 if idea.symbol == Symbol.XAUUSD else 0.03
            check_spread(spread, idea.symbol)
        except Exception as exc:
            logger.warning("Spread check skipped: {}", exc)

        # Check account margin (simplified for demo)
        account = await account_manager.get_account()
        if account.equity < 100:
            raise RiskViolation("Insufficient equity to open position")

        logger.debug("✅ All safeguards passed for {}", idea.symbol.value)

    async def _calculate_position_size(self, idea: TradeIdea) -> float:
        """
        Calculate position size based on risk parameters.
        
        Returns:
            Position size in lots
        """
        account = await account_manager.get_account()
        expansion_state = await storage.load_expansion_state()

        position_calc = risk_manager.compute_position_pnl(
            equity=account.equity,
            consecutive_losses=account.consecutive_losses,
            expansion_active=expansion_state.active,
        )

        # Calculate position size based on SL distance
        risk_amount = abs(position_calc["loss"])
        sl_distance = abs(idea.entry - idea.sl)

        if sl_distance == 0:
            raise RiskViolation("SL distance is zero")

        # For XAUUSD: 1 lot = $1 per point
        # For XAGUSD: 1 lot = $5 per point (assuming standard lot sizing)
        point_value = 1.0 if idea.symbol == Symbol.XAUUSD else 5.0
        position_size = round(risk_amount / (sl_distance * point_value), 2)

        # Enforce minimum and maximum position sizes
        min_size = 0.01
        
        # SAFETY: For demo mode, cap at micro lots to avoid margin issues
        if settings.bnk_test_mode or settings.mode == Mode.DEMO:
            max_size = 0.01  # Micro lot only for testing
            logger.warning("🔒 TEST MODE: Position size capped at 0.01 lots (micro lot)")
        else:
            max_size = 10.0
        
        position_size = max(min_size, min(position_size, max_size))

        logger.debug(
            "Position sizing: Risk=${:.2f} | SL dist={:.4f} | Size={:.2f} lots",
            risk_amount,
            sl_distance,
            position_size,
        )

        return position_size

    # -------------------------------------------------------------------------
    # cTrader API interactions
    # -------------------------------------------------------------------------

    def _build_order_payload(
        self,
        idea: TradeIdea,
        position_size: float,
        current_price: float,
    ) -> dict[str, Any]:
        """
        Build cTrader order payload.
        
        Note: Actual cTrader API format may vary. This is a simplified version.
        Consult cTrader Open API documentation for exact schema.
        """
        # Get account ID from stored secrets
        account_id = settings.ctrader_account_id

        # Convert symbol to cTrader format (e.g., "XAUUSD" -> "XAUUSD")
        symbol_name = idea.symbol.value

        # Convert side to cTrader format
        side = "BUY" if idea.side == Side.BUY else "SELL"

        # Volume in cents (cTrader uses volume in cents, 100 = 0.01 lots)
        volume_cents = int(position_size * 10000)

        return {
            "accountId": account_id,
            "symbolName": symbol_name,
            "tradeSide": side,
            "volume": volume_cents,
            "orderType": "MARKET",
            "comment": f"BNK_TRADE_{idea.id or 'UNKNOWN'}",
        }

    async def _send_market_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Send market order to cTrader API.
        
        Returns:
            Order result with position ID and execution price
        """
        headers = await self._get_headers()

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.api_base}/v2/tradingaccounts/{payload['accountId']}/orders",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()

    async def _attach_sl_tp(self, position_id: str, sl: float, tp: float) -> None:
        """
        Attach or modify SL/TP for an open position.
        
        Args:
            position_id: Remote position ID
            sl: Stop loss price
            tp: Take profit price
        """
        headers = await self._get_headers()
        account_id = settings.ctrader_account_id

        payload = {
            "positionId": position_id,
            "stopLoss": sl,
            "takeProfit": tp,
        }

        logger.debug("Attaching SL/TP to position {}: SL={}, TP={}", position_id, sl, tp)

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.patch(
                    f"{self.api_base}/v2/tradingaccounts/{account_id}/positions/{position_id}",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                logger.success("✅ SL/TP attached to position {}", position_id)
            except Exception as exc:
                logger.error("Failed to attach SL/TP to position {}: {}", position_id, exc)
                raise

    async def _send_close_order(self, position_id: str) -> dict[str, Any]:
        """
        Close an open position.
        
        Args:
            position_id: Remote position ID
        
        Returns:
            Close result
        """
        headers = await self._get_headers()
        account_id = settings.ctrader_account_id

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"{self.api_base}/v2/tradingaccounts/{account_id}/positions/{position_id}",
                headers=headers,
            )
            response.raise_for_status()
            return response.json()

    # -------------------------------------------------------------------------
    # Position ID storage (local <-> remote mapping)
    # -------------------------------------------------------------------------

    async def _store_remote_position_id(self, local_trade_id: int, remote_position_id: str) -> None:
        """Store mapping between local trade ID and remote position ID."""
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO secrets (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (
                    f"position_id_{local_trade_id}",
                    remote_position_id,
                    datetime.utcnow().isoformat(),
                ),
            )
            await conn.commit()

    async def _get_remote_position_id(self, local_trade_id: int | None) -> str | None:
        """
        Retrieve broker position ID for a local trade.

        Priority:
          1. trades.broker_position_id column (set on new executions)
          2. secrets table (legacy mapping from before journal upgrade)
        """
        if not local_trade_id:
            return None

        # 1. Check the trades table first (new canonical location)
        async with aiosqlite.connect(DB_PATH) as conn:
            cur = await conn.execute(
                "SELECT broker_position_id FROM trades WHERE id = ?",
                (local_trade_id,),
            )
            row = await cur.fetchone()
            if row and row[0]:
                return str(row[0])

        # 2. Fall back to legacy secrets table
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT value FROM secrets WHERE key = ?",
                (f"position_id_{local_trade_id}",),
            )
            row = await cursor.fetchone()
            if row and row[0]:
                return str(row[0])

        return None

    # -------------------------------------------------------------------------
    # Position sync (Protobuf TCP — not REST)
    # -------------------------------------------------------------------------

    async def sync_positions(self) -> dict:
        """
        Reconcile local open trades against broker via Protobuf ProtoOAReconcileReq.

        Handles:
        - Remote closures (SL hit, TP hit, manual close)
        - PnL updates for still-open positions
        - Orphaned local trades (no remote ID — tries to mark VOID if broker clean)

        Returns:
            {synced, closed, errors, error_details, closed_trades}
        """
        import traceback
        import ctrader_open_api.messages.OpenApiMessages_pb2 as m

        synced = 0
        closed = 0
        errors = 0
        error_details: list[str] = []
        closed_trades: list = []  # TradeResult objects that closed this tick

        try:
            local_trades = await storage.get_open_trades()
            # Only sync demo/live trades — paper/void are irrelevant to the broker
            live_trades = [t for t in local_trades if t.mode in (Mode.DEMO,)]

            if not live_trades:
                logger.debug("sync_positions: no open demo trades to reconcile")
                return {"synced": 0, "closed": 0, "errors": 0, "error_details": [], "closed_trades": []}

            logger.debug("sync_positions: reconciling {} open demo trade(s)", len(live_trades))

            # Fetch open positions from broker via Protobuf
            from ..integration.ctrader_trading import get_trading_connection
            from ..services.ctrader_oauth import oauth_service

            # ── PHASE 1: OAuth token ──────────────────────────────────────────
            logger.debug("sync_positions [phase=oauth]: fetching valid access token")
            try:
                token = await oauth_service.get_valid_access_token()
            except Exception as token_exc:
                raise BrokerError(
                    f"sync_positions: OAuth token unavailable — {type(token_exc).__name__}: {token_exc}"
                ) from token_exc
            logger.debug("sync_positions [phase=oauth]: token obtained OK")
            # ─────────────────────────────────────────────────────────────────

            account_id = settings.ctrader_account_id
            logger.debug("sync_positions [phase=connect]: account={}", account_id)

            # ── PHASE 2: Broker connection ────────────────────────────────────
            conn = await get_trading_connection()
            logger.debug(
                "sync_positions [phase=connect]: connection alive={}, authenticating account",
                conn.is_connected,
            )
            await conn.authenticate_account(account_id, token)
            logger.debug("sync_positions [phase=connect]: account authenticated")
            # ─────────────────────────────────────────────────────────────────

            # ── PHASE 3: Reconcile request ────────────────────────────────────
            logger.debug("sync_positions [phase=reconcile]: sending ProtoOAReconcileReq")
            req = m.ProtoOAReconcileReq()
            req.ctidTraderAccountId = int(account_id)
            rt, rp = await conn.send_and_wait(req.payloadType, req.SerializeToString(), timeout=10.0)
            logger.debug(
                "sync_positions [phase=reconcile]: broker response payloadType={} len={}",
                rt, len(rp),
            )
            # ─────────────────────────────────────────────────────────────────

            if rt != 2125:
                raw_hex = rp.hex() if rp else "(empty)"
                logger.error(
                    "sync_positions [phase=reconcile]: unexpected payloadType={} raw_bytes={}",
                    rt, raw_hex,
                )
                try:
                    err_res = m.ProtoOAErrorRes()
                    err_res.ParseFromString(rp)
                    raise BrokerError(f"ReconcileReq failed: {err_res.errorCode} {err_res.description}")
                except BrokerError:
                    raise
                except Exception as parse_exc:
                    logger.error(
                        "sync_positions [phase=reconcile]: protobuf parse failed for error response: {}\nraw_hex={}",
                        parse_exc, raw_hex,
                    )
                    raise BrokerError(f"ReconcileReq unexpected payloadType={rt} (parse: {parse_exc})")

            reconcile_res = m.ProtoOAReconcileRes()
            reconcile_res.ParseFromString(rp)

            # Build set of broker positionIds that are still open
            broker_open_ids: set[str] = {
                str(p.positionId) for p in reconcile_res.position
            }
            # Map positionId → swap-adjusted unrealised pnl
            broker_pnl: dict[str, float] = {
                str(p.positionId): p.swap / 100.0  # swap is in cents
                for p in reconcile_res.position
            }
            logger.debug(
                "sync_positions: broker reports {} open position(s): {}",
                len(broker_open_ids), broker_open_ids or "(none)",
            )

            for trade in live_trades:
                try:
                    remote_id = await self._get_remote_position_id(trade.id)
                    logger.debug(
                        "sync_positions: trade_id={} symbol={} remote_id={}",
                        trade.id, trade.symbol.value, remote_id or "(none)",
                    )

                    if not remote_id:
                        # No remote ID stored — trade was opened before journal upgrade
                        # or the broker write failed. If broker has no open positions
                        # we can safely mark it VOID to prevent it blocking future syncs.
                        if not broker_open_ids:
                            trade.outcome = TradeOutcome.VOID
                            trade.ts_close = datetime.now(timezone.utc)
                            logger.debug(
                                "sync_positions [phase=db_upsert]: trade_id={} marking VOID",
                                trade.id,
                            )
                            await storage.update_trade(trade)
                            logger.debug(
                                "sync_positions [phase=db_commit]: trade_id={} VOID committed",
                                trade.id,
                            )
                            logger.warning(
                                "sync: trade_id={} ({} {}): no remote ID + broker clean — marked VOID (not an error)",
                                trade.id, trade.symbol.value, trade.side.value,
                            )
                            # Broker is clean and we resolved the trade — count as closed, NOT an error.
                            closed_trades.append(trade)
                            closed += 1
                        else:
                            detail = (
                                f"trade_id={trade.id} ({trade.symbol.value} {trade.side.value}): "
                                f"no remote ID stored — cannot reconcile "
                                f"(broker has {len(broker_open_ids)} open position(s))"
                            )
                            logger.error("sync: {}", detail)
                            error_details.append(detail)
                            errors += 1
                        continue

                    if remote_id in broker_open_ids:
                        # Still open — update unrealised PnL
                        trade.pnl = broker_pnl.get(remote_id, 0.0)
                        logger.debug(
                            "sync_positions: trade_id={} still open, swap_pnl={:+.2f}",
                            trade.id, trade.pnl,
                        )
                        logger.debug(
                            "sync_positions [phase=db_upsert]: trade_id={} updating open PnL",
                            trade.id,
                        )
                        await storage.update_trade(trade)
                        logger.debug(
                            "sync_positions [phase=db_commit]: trade_id={} PnL commit OK",
                            trade.id,
                        )
                        synced += 1
                    else:
                        # Closed remotely (SL/TP hit or manual)
                        logger.debug(
                            "sync_positions: trade_id={} remote_id={} NOT in broker — fetching deal info",
                            trade.id, remote_id,
                        )
                        deal_info = await self._get_closed_deal_info(conn, account_id, remote_id)
                        final_pnl = deal_info["pnl"]
                        exit_px = deal_info["exit_price"]
                        logger.debug(
                            "sync_positions: trade_id={} deal pnl={:+.2f} exit_px={}",
                            trade.id, final_pnl, exit_px,
                        )

                        trade.pnl = final_pnl
                        trade.ts_close = datetime.now(timezone.utc)
                        trade.outcome = TradeOutcome.WIN if final_pnl > 0 else TradeOutcome.LOSS
                        trade.exit_price = exit_px

                        # Exit slippage: actual exit vs TP/SL reference
                        if exit_px is not None:
                            ref_price = trade.tp if final_pnl > 0 else trade.sl
                            if trade.side == Side.BUY:
                                trade.exit_slippage = round(ref_price - exit_px, 5)
                            else:
                                trade.exit_slippage = round(exit_px - ref_price, 5)

                        logger.debug(
                            "sync_positions [phase=db_upsert]: trade_id={} writing close outcome={} pnl={:+.2f}",
                            trade.id, trade.outcome.value, final_pnl,
                        )
                        await storage.update_trade(trade)
                        logger.debug(
                            "sync_positions [phase=db_commit]: trade_id={} close committed",
                            trade.id,
                        )

                        # Record in risk engine so daily counters stay accurate
                        await locks.record_trade(final_pnl, is_loss=(final_pnl <= 0))

                        logger.success(
                            "📊 Broker-closed: trade_id={} remote_id={} outcome={} pnl={:+.2f}",
                            trade.id, remote_id, trade.outcome.value, final_pnl,
                        )
                        closed_trades.append(trade)
                        closed += 1
                        # Audit log
                        import json as _json
                        await storage.log_execution_event(
                            "position_closed",
                            trade_id=trade.id,
                            symbol=trade.symbol.value,
                            detail=_json.dumps({
                                "outcome": trade.outcome.value,
                                "pnl": final_pnl,
                                "exit_price": exit_px,
                                "remote_id": remote_id,
                            }),
                        )

                except Exception as exc:
                    tb = traceback.format_exc()
                    detail = f"trade_id={trade.id}: {type(exc).__name__}: {exc}"
                    logger.error("sync error: {}\n{}", detail, tb)
                    error_details.append(detail)
                    errors += 1
                    await storage.log_execution_event(
                        "sync_error",
                        trade_id=trade.id,
                        symbol=trade.symbol.value if hasattr(trade, 'symbol') else None,
                        detail=detail[:500],
                    )

            logger.info(
                "sync_positions complete: synced={} closed={} errors={}",
                synced, closed, errors,
            )
            return {
                "synced": synced,
                "closed": closed,
                "errors": errors,
                "error_details": error_details,
                "closed_trades": closed_trades,
            }

        except Exception as exc:
            tb = traceback.format_exc()
            detail = f"sync_positions outer: {type(exc).__name__}: {exc}"
            logger.error("{}\n{}", detail, tb)
            error_details.append(detail)
            try:
                await storage.log_execution_event("sync_error", detail=detail[:500])
            except Exception:
                pass
            return {
                "synced": 0,
                "closed": 0,
                "errors": errors + 1,
                "error_details": error_details,
                "closed_trades": [],
            }

    async def _get_closed_deal_info(self, conn, account_id: str, position_id: str) -> dict:
        """
        Fetch final PnL AND execution price for a broker-closed position
        via ProtoOADealListReq (payloadType 2152, response 2154).

        Returns:
            {"pnl": float, "exit_price": float | None}
        """
        try:
            import traceback as _tb
            import ctrader_open_api.messages.OpenApiMessages_pb2 as m

            to_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
            from_ts = to_ts - 86_400_000  # look back 24 h

            req = m.ProtoOADealListReq()
            req.ctidTraderAccountId = int(account_id)
            req.fromTimestamp = from_ts
            req.toTimestamp = to_ts
            logger.debug(
                "_get_closed_deal_info: position_id={} from_ts={} to_ts={}",
                position_id, from_ts, to_ts,
            )
            rt, rp = await conn.send_and_wait(req.payloadType, req.SerializeToString(), timeout=10.0)
            logger.debug(
                "_get_closed_deal_info: DealListReq response payloadType={} len={}",
                rt, len(rp),
            )

            if rt != 2154:
                logger.error(
                    "_get_closed_deal_info: unexpected payloadType={} raw_bytes={}",
                    rt, rp.hex() if rp else "(empty)",
                )
                return {"pnl": 0.0, "exit_price": None}

            if rt == 2154:
                res = m.ProtoOADealListRes()
                res.ParseFromString(rp)
                total_pnl = 0.0
                exit_price: float | None = None
                found = False
                for deal in res.deal:
                    if str(deal.positionId) == position_id and deal.closePositionDetail:
                        cpd = deal.closePositionDetail
                        total_pnl += cpd.grossProfit / 100.0 + cpd.swap / 100.0 + deal.commission / 100.0
                        # executionPrice is in price (division by 100000 not needed for FX/metals)
                        if deal.executionPrice:
                            exit_price = deal.executionPrice / 100000.0
                        found = True
                if found:
                    logger.debug(
                        "_get_closed_deal_info: position_id={} pnl={:+.2f} exit_px={}",
                        position_id, total_pnl, exit_price,
                    )
                    return {"pnl": total_pnl, "exit_price": exit_price}
                else:
                    logger.warning(
                        "_get_closed_deal_info: position_id={} — no matching close deals found in {} deals",
                        position_id, len(res.deal),
                    )
        except Exception as exc:
            logger.error(
                "DealListReq failed for position {}:\n{}",
                position_id, _tb.format_exc(),
            )
        return {"pnl": 0.0, "exit_price": None}

    # Keep old name as alias for backward compat
    async def _get_closed_deal_pnl(self, conn, account_id: str, position_id: str) -> float:
        info = await self._get_closed_deal_info(conn, account_id, position_id)
        return info["pnl"]

    async def _get_latest_spread(self, symbol: Symbol) -> float | None:
        """Look up the most recent spread for a symbol from the ticks table."""
        try:
            async with aiosqlite.connect(DB_PATH) as conn:
                cur = await conn.execute(
                    "SELECT spread FROM ticks WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                    (symbol.value,),
                )
                row = await cur.fetchone()
                if row:
                    return row[0]
        except Exception as exc:
            logger.debug("spread lookup failed for {}: {}", symbol.value, exc)
        # Fallback to asset-class default
        return 0.30 if symbol == Symbol.XAUUSD else 0.03




# Singleton instance
ctrader_executor = CTraderExecutionService()
