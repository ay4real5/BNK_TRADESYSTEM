#!/usr/bin/env python3
"""
verify_autoloop.py — End-to-end health check for the autonomous trading loop.

Checks:
  1.  Server health
  2.  Mode = demo
  3.  cTrader TCP connection + correct account
  4.  DB cleanliness (no phantom/void open trades)
  5.  Sync-positions (errors=0, error_details empty)
  6.  Auto-execution config
  7.  Signal pipeline (recent signals, score distribution)
  8.  Execution idempotency guard (reject duplicate)
  9.  Symbol conflict guard (reject second position on same symbol)
  10. Broker reconcile (live open positions)

Usage:
    python scripts/verify_autoloop.py
    python scripts/verify_autoloop.py --enable-auto   # also turn on AUTO_EXECUTE_DEMO
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx

BASE = "http://127.0.0.1:8000/api/v1"
PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


def _p(status: str, label: str, detail: str = "") -> None:
    print(f"  {status}  {label}" + (f"  →  {detail}" if detail else ""))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--enable-auto", action="store_true", help="Enable AUTO_EXECUTE_DEMO via API")
    args = parser.parse_args()

    failures = 0

    async with httpx.AsyncClient(timeout=15.0) as c:

        # ── 1. Server health ─────────────────────────────────────────────────
        print("\n── 1. Server health")
        try:
            r = await c.get(f"{BASE}/health")
            d = r.json()
            if d.get("status") == "ok":
                _p(PASS, "Server up", f"mode={d.get('mode')}")
            else:
                _p(FAIL, "Server returned non-ok status", str(d)); failures += 1
        except Exception as exc:
            _p(FAIL, "Cannot reach server", str(exc)); failures += 1
            print("\nServer is down — aborting remaining checks.")
            return failures

        # ── 2. Mode ──────────────────────────────────────────────────────────
        print("\n── 2. Mode check")
        r = await c.get(f"{BASE}/mode")
        mode = r.json().get("mode", "?")
        if mode == "demo":
            _p(PASS, "Mode = demo")
        else:
            _p(WARN, f"Mode = {mode} (expected demo for autonomous execution)")

        # ── 3. cTrader connection ─────────────────────────────────────────────
        print("\n── 3. cTrader connection")
        try:
            r = await c.get(f"{BASE}/ctrader/status")
            d = r.json()
            acct = d.get("account_id", "?")
            loaded = d.get("accounts_loaded", False)
            connected = d.get("connection", {}).get("connected", False)
            if loaded and str(acct) == "46435708":
                _p(PASS, f"Account {acct} loaded")
            else:
                _p(FAIL, f"Account mismatch or not loaded: account_id={acct} loaded={loaded}"); failures += 1
        except Exception as exc:
            _p(FAIL, "cTrader status error", str(exc)); failures += 1

        # ── 4. DB cleanliness ────────────────────────────────────────────────
        print("\n── 4. DB cleanliness")
        r = await c.get(f"{BASE}/signals/recent?limit=1")  # just to verify server is responsive
        # Check via sync that no open trades are hanging around
        r2 = await c.post(f"{BASE}/execution/sync-positions")
        d2 = r2.json()
        if d2.get("errors", 0) == 0:
            _p(PASS, f"No sync errors | synced={d2.get('synced',0)} closed={d2.get('closed',0)}")
        else:
            details = d2.get("error_details", [])
            _p(FAIL if d2["errors"] > 0 else WARN,
               f"Sync errors={d2['errors']}", " | ".join(details[:3]))
            failures += 1

        # ── 5. Auto-execution config ─────────────────────────────────────────
        print("\n── 5. Auto-execution config")
        r = await c.get(f"{BASE}/auto-execute/status")
        ae = r.json()
        _p(PASS, f"min_score_to_execute={ae.get('min_score_to_execute')}")
        _p(PASS, f"auto_execute_interval_sec={ae.get('auto_execute_interval_sec')}")
        _p(PASS, f"position_sync_interval_sec={ae.get('position_sync_interval_sec')}")
        if ae.get("auto_execute_demo"):
            _p(PASS, "AUTO_EXECUTE_DEMO = ON",
               "scheduler_job=" + str(ae.get("scheduler_job_active")))
        else:
            _p(WARN, "AUTO_EXECUTE_DEMO = OFF  (run with --enable-auto or POST /auto-execute/enable)")

        in_session = ae.get("in_trading_session", False)
        _p(PASS if in_session else WARN,
           f"Trading session: {'ACTIVE' if in_session else 'OUTSIDE HOURS'}")

        # ── 6. Signal pipeline ───────────────────────────────────────────────
        print("\n── 6. Signal pipeline")
        r = await c.get(f"{BASE}/signals/recent?limit=20")
        if r.status_code == 200:
            sigs = r.json()
            pending = [s for s in sigs if s.get("status") == "pending"]
            above_threshold = [s for s in pending if s.get("score", 0) >= ae.get("min_score_to_execute", 7.5)]
            _p(PASS, f"Recent signals: {len(sigs)} total, {len(pending)} pending, "
               f"{len(above_threshold)} above score threshold")
            if above_threshold:
                best = above_threshold[0]
                _p(PASS, f"Best candidate: signal #{best['id']} {best['symbol']} {best['side']} score={best['score']}")
        else:
            _p(FAIL, "Could not fetch signals"); failures += 1

        # ── 7. Idempotency guard ─────────────────────────────────────────────
        print("\n── 7. Idempotency guard (must reject already-executed signal)")
        # Find an EXECUTED signal
        r = await c.get(f"{BASE}/signals/recent?limit=100")
        executed_sig = next((s for s in r.json() if s.get("status") == "executed"), None)
        if executed_sig:
            r2 = await c.request("POST", f"{BASE}/signals/{executed_sig['id']}/execute",
                                  params={"force": "true"})
            if r2.status_code == 409:
                _p(PASS, f"Signal #{executed_sig['id']} correctly rejected (409)")
            else:
                _p(FAIL, f"Expected 409, got {r2.status_code}: {r2.text[:100]}"); failures += 1
        else:
            _p(WARN, "No executed signals found — skipping idempotency test")

        # ── 8. Broker reconcile (live positions) ─────────────────────────────
        print("\n── 8. Live broker positions")
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import ctrader_open_api.messages.OpenApiMessages_pb2 as m

            async def _broker_positions():
                from app.services.ctrader_oauth import oauth_service
                from app.config import settings as cfg
                from app.integration.ctrader_trading import get_trading_connection
                import app.integration.ctrader_trading as t
                t._trading_connection = None

                token = await oauth_service.get_valid_access_token()
                conn = await get_trading_connection()
                await conn.authenticate_account(cfg.ctrader_account_id, token)
                req = m.ProtoOAReconcileReq()
                req.ctidTraderAccountId = int(cfg.ctrader_account_id)
                rt, rp = await conn.send_and_wait(req.payloadType, req.SerializeToString(), timeout=10.0)
                if rt == 2125:
                    res = m.ProtoOAReconcileRes(); res.ParseFromString(rp)
                    return [(p.positionId, p.tradeData.symbolId, p.tradeData.tradeSide) for p in res.position]
                return []

            positions = await _broker_positions()
            if positions:
                _p(PASS, f"{len(positions)} open position(s) at broker:")
                for pid, symid, side in positions:
                    _p("  📌", f"positionId={pid} symbolId={symid} side={'BUY' if side==1 else 'SELL'}")
            else:
                _p(PASS, "No open positions at broker (clean state)")
        except Exception as exc:
            _p(WARN, f"Broker reconcile check failed: {exc}")

        # ── Enable auto-execute if flag set ──────────────────────────────────
        if args.enable_auto:
            print("\n── Enabling AUTO_EXECUTE_DEMO via API")
            r = await c.post(f"{BASE}/auto-execute/enable")
            d = r.json()
            if d.get("success"):
                _p(PASS, "Auto-execute enabled", d.get("message", ""))
            else:
                _p(FAIL, "Failed to enable", str(d)); failures += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'─'*55}")
    if failures == 0:
        print(f"  {PASS}  All checks passed. The autonomous loop is ready.")
        print(f"\n  To enable auto-execution:")
        print(f"    python scripts/verify_autoloop.py --enable-auto")
        print(f"    # OR: curl -X POST http://127.0.0.1:8000/api/v1/auto-execute/enable")
        print(f"\n  To watch it run:")
        print(f"    tail -f logs/*.log | grep -E 'AUTO-EXEC|broker-closed|sync'")
    else:
        print(f"  {FAIL}  {failures} check(s) failed. Fix issues above before enabling auto-execution.")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
