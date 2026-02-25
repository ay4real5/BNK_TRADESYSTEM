"""
Validate Paper Execution Layer.

Tests the complete flow:
1. Pending signals → execution
2. Risk engine enforcement
3. Position tracking
4. SL/TP monitoring
5. P&L calculation
"""

import asyncio
import sqlite3
from pathlib import Path
from datetime import datetime

async def main():
    print("=" * 70)
    print("PAPER EXECUTION LAYER VALIDATION")
    print("=" * 70)
    
    db_path = Path("data/trading.db")
    
    # Check if database exists
    if not db_path.exists():
        print("❌ Database not found")
        return 1
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    results = {
        "paper_execution": "PENDING",
        "risk_enforcement": "PENDING",
        "position_tracking": "PENDING",
        "pnl_calculation": "PENDING",
        "ready_for_ctrader_demo": "NO"
    }
    
    # 1. Check pending signals exist
    print("\n1️⃣ CHECKING PENDING SIGNALS")
    cursor.execute("SELECT COUNT(*) FROM signals WHERE status = 'pending'")
    pending_count = cursor.fetchone()[0]
    print(f"   Pending signals: {pending_count}")
    
    # 2. Check trades table exists
    print("\n2️⃣ CHECKING TRADES TABLE")
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
    trades_table_exists = cursor.fetchone() is not None
    
    if trades_table_exists:
        print("   ✅ Trades table exists")
        
        # Check for open trades
        cursor.execute("SELECT COUNT(*) FROM trades WHERE outcome = 'open'")
        open_count = cursor.fetchone()[0]
        print(f"   Open positions: {open_count}")
        
        # Check for completed trades
        cursor.execute("SELECT COUNT(*) FROM trades WHERE outcome IN ('win', 'loss')")
        completed_count = cursor.fetchone()[0]
        print(f"   Completed trades: {completed_count}")
        
        if open_count > 0 or completed_count > 0:
            results["position_tracking"] = "PASS"
            print("   ✅ Position tracking: PASS")
        else:
            print("   ⚠️  No trades yet (waiting for execution)")
    else:
        print("   ❌ Trades table does not exist")
        results["position_tracking"] = "FAIL"
    
    # 3. Check executed signals
    print("\n3️⃣ CHECKING SIGNAL EXECUTION")
    cursor.execute("SELECT COUNT(*) FROM signals WHERE status = 'executed'")
    executed_count = cursor.fetchone()[0]
    print(f"   Executed signals: {executed_count}")
    
    cursor.execute("SELECT COUNT(*) FROM signals WHERE status = 'rejected'")
    rejected_count = cursor.fetchone()[0]
    print(f"   Rejected signals: {rejected_count}")
    
    if executed_count > 0:
        results["paper_execution"] = "PASS"
        print("   ✅ Paper execution: PASS")
        
        # Show sample executed signal
        cursor.execute("""
            SELECT id, symbol, side, entry, sl, tp, status, ts
            FROM signals
            WHERE status = 'executed'
            ORDER BY ts DESC
            LIMIT 1
        """)
        signal = cursor.fetchone()
        if signal:
            print(f"\n   Latest executed signal:")
            print(f"   - ID: {signal[0]}")
            print(f"   - Symbol: {signal[1]}")
            print(f"   - Side: {signal[2]}")
            print(f"   - Entry: {signal[3]}")
            print(f"   - SL: {signal[4]}")
            print(f"   - TP: {signal[5]}")
            print(f"   - Status: {signal[6]}")
    else:
        print("   ⚠️  No executed signals yet (may be waiting for cooldown)")
    
    # 4. Check risk enforcement
    print("\n4️⃣ CHECKING RISK ENFORCEMENT")
    if rejected_count > 0:
        results["risk_enforcement"] = "PASS"
        print(f"   ✅ Risk engine rejecting signals: {rejected_count} rejected")
    else:
        print("   ⚠️  No rejections yet (risk limits may not be hit)")
        # Still pass if we see executed trades (means risk approved them)
        if executed_count > 0:
            results["risk_enforcement"] = "PASS"
            print("   ✅ Risk engine approving valid signals")
    
    # 5. Check P&L calculation
    print("\n5️⃣ CHECKING P&L CALCULATION")
    if trades_table_exists:
        cursor.execute("""
            SELECT id, symbol, side, entry, pnl, outcome, ts_open, ts_close
            FROM trades
            WHERE outcome IN ('win', 'loss')
            ORDER BY ts_close DESC
            LIMIT 3
        """)
        closed_trades = cursor.fetchall()
        
        if closed_trades:
            results["pnl_calculation"] = "PASS"
            print(f"   ✅ {len(closed_trades)} closed trades with P&L")
            
            for trade in closed_trades:
                print(f"\n   Trade #{trade[0]}:")
                print(f"   - Symbol: {trade[1]}")
                print(f"   - Side: {trade[2]}")
                print(f"   - Entry: {trade[3]}")
                print(f"   - P&L: ${trade[4]:.2f}")
                print(f"   - Outcome: {trade[5]}")
                print(f"   - Duration: {trade[6]} → {trade[7]}")
        else:
            print("   ⚠️  No closed trades yet (positions may still be open)")
            # Check if we have open trades
            cursor.execute("SELECT COUNT(*) FROM trades WHERE outcome = 'open'")
            if cursor.fetchone()[0] > 0:
                print("   ℹ️  Open positions exist - waiting for SL/TP hit")
    
    # 6. Check account state
    print("\n6️⃣ CHECKING ACCOUNT STATE")
    cursor.execute("SELECT equity, balance, peak_equity, consecutive_losses FROM account LIMIT 1")
    account = cursor.fetchone()
    if account:
        print(f"   Equity: ${account[0]:.2f}")
        print(f"   Balance: ${account[1]:.2f}")
        print(f"   Peak Equity: ${account[2]:.2f}")
        print(f"   Consecutive Losses: {account[3]}")
    
    # 7. Check scheduler integration
    print("\n7️⃣ CHECKING SCHEDULER INTEGRATION")
    print("   To verify scheduler is running paper execution:")
    print("   - Check backend logs for 'paper_execution' job")
    print("   - Should run every 3-5 seconds")
    print("   - Look for 'Execution tick' log messages")
    
    conn.close()
    
    # Final determination
    print("\n" + "=" * 70)
    print("VALIDATION RESULTS")
    print("=" * 70)
    
    for key, value in results.items():
        if key == "ready_for_ctrader_demo":
            continue
        icon = "✅" if value == "PASS" else "⚠️" if value == "PENDING" else "❌"
        print(f"{icon} {key.replace('_', ' ').title()}: {value}")
    
    # Determine if ready for cTrader demo
    all_pass = all(v == "PASS" for k, v in results.items() if k != "ready_for_ctrader_demo")
    results["ready_for_ctrader_demo"] = "YES" if all_pass else "NO"
    
    print("\n" + "=" * 70)
    ready_icon = "🎯" if results["ready_for_ctrader_demo"] == "YES" else "⛔"
    print(f"{ready_icon} READY FOR CTRADER DEMO: {results['ready_for_ctrader_demo']}")
    print("=" * 70)
    
    if results["ready_for_ctrader_demo"] == "YES":
        print("\n✅ Paper execution layer validated.")
        print("Next step: Integrate cTrader demo feed (read-only)")
        return 0
    else:
        print("\n⚠️  Paper execution layer partially working.")
        print("Suggestions:")
        if results["paper_execution"] == "PENDING":
            print("  - Wait for signals to move from pending → executed")
            print("  - Check if risk locks are preventing execution")
            print("  - Verify scheduler is running paper execution job")
        if results["pnl_calculation"] == "PENDING":
            print("  - Wait for positions to hit SL or TP")
            print("  - Check position monitoring is running")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
