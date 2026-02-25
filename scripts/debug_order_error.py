#!/usr/bin/env python3
"""
debug_order_error.py
====================
Full execution lifecycle test:

  1. Connect  → App auth  → Account auth
  2. Verify symbol (non-fatal)
  3. Send ONE MARKET BUY order
  4. Wait for ORDER_ACCEPTED  (executionType=2)
  5. Wait for ORDER_FILLED    (executionType=3) — or fall back to Reconcile
  6. Print confirmed position: positionId, symbolId, volume, entryPrice, side
  7. Verdict: PASS / FAIL

Run:
    python scripts/debug_order_error.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from app.config import settings
from app.domain.enums import Symbol, Side
from app.services.ctrader_oauth import oauth_service
from app.integration.ctrader_trading import (
    CTraderTradingConnection,
    PROTO_OA_ORDER_ERROR_EVENT,
    PROTO_OA_EXECUTION_EVENT,
    _EXECUTION_TYPE_NAMES,
    _SYMBOL_ID_MAP,
)

logger.remove()
logger.add(sys.stdout, level="DEBUG", colorize=False,
           format="{time:HH:mm:ss.SSS} | {level:<8} | {message}")

SEP  = "═" * 80
SEP2 = "─" * 80


async def run_debug_order():
    print()
    print(SEP)
    print("  BNK TRADE SYSTEM — FULL EXECUTION LIFECYCLE TEST")
    print(f"  cTrader env  : {settings.ctrader_env}")
    print(f"  Account ID   : {settings.ctrader_account_id}")
    print(f"  Test mode    : {settings.bnk_test_mode}")
    print(SEP)
    print()

    # 1 ── OAuth token ────────────────────────────────────────────────────────
    logger.info("Step 1/6 — Fetching OAuth access token ...")
    try:
        access_token = await oauth_service.get_valid_access_token()
        logger.info(f"  token: {access_token[:20]}... (len={len(access_token)})")
    except Exception as exc:
        logger.error(f"FATAL: Cannot get access token: {exc}")
        return

    account_id = str(settings.ctrader_account_id)
    symbol     = Symbol.XAUUSD
    symbol_id  = _SYMBOL_ID_MAP.get(symbol, 41)
    VOLUME     = 1000   # 0.01 lots (100_000-per-lot convention)

    # 2 ── TCP connection ─────────────────────────────────────────────────────
    logger.info("Step 2/6 — Connecting to cTrader TCP ...")
    host = "demo.ctraderapi.com" if settings.ctrader_env == "demo" else "live.ctraderapi.com"
    conn = CTraderTradingConnection(
        host=host, port=5035,
        client_id=settings.ctrader_client_id,
        client_secret=settings.ctrader_client_secret,
    )
    try:
        await conn.connect()
    except Exception as exc:
        logger.error(f"FATAL: connection failed: {exc}")
        return

    # 3 ── Auth ───────────────────────────────────────────────────────────────
    logger.info("Step 3/6 — App + account auth ...")
    try:
        await conn.authenticate_application()
        await conn.authenticate_account(account_id, access_token)
    except Exception as exc:
        logger.error(f"FATAL: auth failed: {exc}")
        await conn.disconnect()
        return

    # 4 ── Symbol verify (non-fatal) ──────────────────────────────────────────
    logger.info(f"Step 4/6 — Symbol verify symbolId={symbol_id} ...")
    await conn.verify_symbol(account_id, symbol_id)

    # 5 ── Place order (ACCEPTED + FILLED + Reconcile) ────────────────────────
    logger.info(f"Step 5/6 — Place MARKET BUY: symbolId={symbol_id} volume={VOLUME}")
    print()

    result: dict | None = None
    try:
        result = await conn.place_market_order(
            account_id=account_id,
            symbol_id=symbol_id,
            side=Side.BUY,
            volume=VOLUME,
        )
    except Exception as exc:
        print()
        print(SEP)
        print("  ❌  ORDER FAILED")
        print(f"     Exception : {type(exc).__name__}: {exc}")
        print(SEP)
        print()
        print("  ► See ORDER_ERROR_EVENT lines above for broker errorCode + description.")
        await conn.disconnect()
        return

    # 6 ── Verdict ────────────────────────────────────────────────────────────
    print()
    print(SEP)
    exec_type   = result.get("executionType", "UNKNOWN")
    entry_price = result.get("executedPrice")
    pos_id      = result.get("positionId")
    order_id    = result.get("orderId")
    vol_conf    = result.get("volume", VOLUME)
    sym_conf    = result.get("symbolId", symbol_id)
    side_name   = {1: "BUY", 2: "SELL"}.get(result.get("tradeSide"), "BUY")

    print("  EXECUTION LIFECYCLE SUMMARY")
    print(SEP2)
    print(f"  positionId    : {pos_id}")
    print(f"  orderId       : {order_id}")
    print(f"  symbolId      : {sym_conf}  (expected {symbol_id} = {symbol.value})")
    print(f"  volume        : {vol_conf}")
    print(f"  tradeSide     : {side_name}")
    print(f"  entryPrice    : {entry_price}")
    print(f"  executionType : {exec_type}")

    # PASS criteria
    has_position = bool(pos_id)
    price_known  = entry_price is not None

    print()
    print(f"  has positionId  : {'✅ YES' if has_position else '❌ NO'}")
    print(f"  entryPrice set  : {'✅ YES  ' + str(entry_price) if price_known else '⚠️  None (position open, price not yet confirmed)'}")
    print()

    if has_position:
        print("  ▶ VERDICT: ✅  FULL EXECUTION LIFECYCLE — PASS")
        print("     Order placed, accepted, and position confirmed on broker.")
        if not price_known:
            print("     Entry price will be visible in cTrader platform and next reconcile poll.")
    else:
        print("  ▶ VERDICT: ❌  EXECUTION FAILED — no positionId returned")
    print(SEP)

    # 6b — Standalone reconcile to double-check ───────────────────────────────
    print()
    logger.info("Step 6/6 — Standalone reconcile (final position snapshot) ...")
    await asyncio.sleep(0.5)
    try:
        positions = await conn.reconcile_positions(account_id)
        print()
        print(SEP)
        print(f"  OPEN POSITIONS (reconcile) — total: {len(positions)}")
        print(SEP2)
        for i, p in enumerate(positions):
            s = {1: "BUY", 2: "SELL"}.get(p["tradeSide"], "?")
            print(f"  [{i+1}] positionId={p['positionId']:>10}  symbolId={p['symbolId']:>5}  "
                  f"volume={p['volume']:>6}  side={s}  entryPrice={p['entryPrice']}  "
                  f"SL={p['stopLoss']}  TP={p['takeProfit']}")
        if not positions:
            print("  (no open positions)")
        print(SEP)
    except Exception as exc:
        logger.warning(f"Standalone reconcile failed: {exc}")

    await conn.disconnect()
    logger.info("Disconnected. Test complete.")


if __name__ == "__main__":
    asyncio.run(run_debug_order())

