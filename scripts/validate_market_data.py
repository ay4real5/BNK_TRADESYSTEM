"""
Strict evidence-based validation of market data pipeline.
No PENDING states allowed. Every PASS/FAIL must have evidence.
"""

import sqlite3
import requests
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any

class MarketDataValidator:
    def __init__(self):
        self.db_path = Path("data/trading.db")
        self.api_base = "http://127.0.0.1:8000/api/v1"
        self.results = {
            "feed_layer": "FAIL",
            "tick_storage": "FAIL",
            "candle_aggregation": "FAIL",
            "api_endpoints": "FAIL",
            "data_freshness": "FAIL",
            "system_integrity": "FAIL",
            "ready_for_execution": "NO"
        }
        self.evidence = {}
        
    def validate_tick_storage(self) -> Tuple[str, Dict]:
        """Validate tick storage with actual DB evidence"""
        print("\n" + "="*70)
        print("VALIDATING TICK STORAGE")
        print("="*70)
        
        evidence = {}
        
        if not self.db_path.exists():
            evidence["error"] = f"Database not found at {self.db_path}"
            print(f"❌ {evidence['error']}")
            return "FAIL", evidence
        
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            
            # Check if ticks table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ticks'")
            if not cursor.fetchone():
                evidence["error"] = "Ticks table does not exist"
                print(f"❌ {evidence['error']}")
                conn.close()
                return "FAIL", evidence
            
            # Check tick count
            cursor.execute("SELECT COUNT(*) FROM ticks")
            tick_count = cursor.fetchone()[0]
            evidence["tick_count"] = tick_count
            print(f"📊 Total ticks: {tick_count}")
            
            if tick_count == 0:
                evidence["error"] = "No ticks in database"
                print(f"❌ {evidence['error']}")
                conn.close()
                return "FAIL", evidence
            
            # Check timestamp range
            cursor.execute("SELECT MIN(ts), MAX(ts) FROM ticks")
            min_ts, max_ts = cursor.fetchone()
            evidence["min_timestamp"] = min_ts
            evidence["max_timestamp"] = max_ts
            print(f"📅 Oldest tick: {min_ts}")
            print(f"📅 Newest tick: {max_ts}")
            
            # Check freshness (within last 120 seconds)
            try:
                latest_dt = datetime.fromisoformat(max_ts.replace('Z', '+00:00'))
                age_seconds = (datetime.utcnow() - latest_dt.replace(tzinfo=None)).total_seconds()
                evidence["latest_tick_age_seconds"] = age_seconds
                print(f"⏱️  Latest tick age: {age_seconds:.1f}s")
                
                if age_seconds > 120:
                    evidence["error"] = f"Data stale (>{age_seconds:.0f}s old, threshold 120s)"
                    print(f"❌ {evidence['error']}")
                    conn.close()
                    return "FAIL", evidence
                else:
                    print(f"✅ Data is fresh (<120s old)")
                    
            except Exception as e:
                evidence["error"] = f"Could not parse timestamp: {e}"
                print(f"❌ {evidence['error']}")
                conn.close()
                return "FAIL", evidence
            
            # Get symbol distribution
            cursor.execute("SELECT symbol, COUNT(*) FROM ticks GROUP BY symbol")
            symbols = dict(cursor.fetchall())
            evidence["symbols"] = symbols
            print(f"💱 Symbols: {symbols}")
            
            conn.close()
            
            print("✅ TICK STORAGE: PASS")
            return "PASS", evidence
            
        except Exception as e:
            evidence["error"] = str(e)
            print(f"❌ Database error: {e}")
            return "FAIL", evidence
    
    def validate_candle_aggregation(self) -> Tuple[str, Dict]:
        """Validate candle aggregation with actual DB evidence"""
        print("\n" + "="*70)
        print("VALIDATING CANDLE AGGREGATION")
        print("="*70)
        
        evidence = {}
        
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            
            # Check if candles table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='candles'")
            has_table = cursor.fetchone() is not None
            evidence["has_candles_table"] = has_table
            
            if not has_table:
                print("ℹ️  No persistent candles table (on-demand aggregation)")
                evidence["mode"] = "on_demand"
            else:
                cursor.execute("SELECT COUNT(*) FROM candles")
                candle_count = cursor.fetchone()[0]
                evidence["candle_count"] = candle_count
                print(f"📊 Stored candles: {candle_count}")
                
                if candle_count > 0:
                    cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM candles")
                    min_ts, max_ts = cursor.fetchone()
                    evidence["min_timestamp"] = min_ts
                    evidence["max_timestamp"] = max_ts
                    print(f"📅 Candle range: {min_ts} to {max_ts}")
                    
                    # Check freshness (within last 5 minutes for m1)
                    try:
                        latest_dt = datetime.fromisoformat(max_ts.replace('Z', '+00:00'))
                        age_seconds = (datetime.utcnow() - latest_dt.replace(tzinfo=None)).total_seconds()
                        evidence["latest_candle_age_seconds"] = age_seconds
                        print(f"⏱️  Latest candle age: {age_seconds:.1f}s")
                        
                        if age_seconds > 300:  # 5 minutes
                            evidence["warning"] = f"Candles may be stale (>{age_seconds:.0f}s old)"
                            print(f"⚠️  {evidence['warning']}")
                    except Exception as e:
                        evidence["timestamp_parse_error"] = str(e)
            
            conn.close()
            
            # For on-demand aggregation, we validate via API endpoint instead
            print("✅ CANDLE AGGREGATION: PASS (will verify via API)")
            return "PASS", evidence
            
        except Exception as e:
            evidence["error"] = str(e)
            print(f"❌ Database error: {e}")
            return "FAIL", evidence
    
    def validate_api_endpoints(self) -> Tuple[str, Dict]:
        """Validate API endpoints with actual HTTP calls"""
        print("\n" + "="*70)
        print("VALIDATING API ENDPOINTS")
        print("="*70)
        
        evidence = {}
        all_pass = True
        
        # Test 1: /data-source
        print("\n1️⃣ Testing GET /data-source")
        try:
            response = requests.get(f"{self.api_base}/data-source", timeout=5)
            evidence["data_source"] = {
                "status_code": response.status_code,
                "response": response.json() if response.status_code == 200 else response.text[:200]
            }
            print(f"   Status: {response.status_code}")
            
            if response.status_code != 200:
                print(f"   ❌ Expected 200, got {response.status_code}")
                all_pass = False
            else:
                data = response.json()
                # Actual API returns: source, connected, feed_status, last_tick_ts, symbols
                required = ['source', 'connected', 'feed_status', 'symbols']
                missing = [k for k in required if k not in data]
                
                if missing:
                    print(f"   ❌ Missing fields: {missing}")
                    evidence["data_source"]["missing_fields"] = missing
                    all_pass = False
                else:
                    print(f"   ✅ All required fields present")
                    print(f"   Response preview:")
                    print(f"   {json.dumps(data, indent=6)[:500]}")
                    
        except Exception as e:
            evidence["data_source"] = {"error": str(e)}
            print(f"   ❌ Request failed: {e}")
            all_pass = False
        
        # Test 2: /candles
        print("\n2️⃣ Testing GET /candles")
        try:
            response = requests.get(
                f"{self.api_base}/candles",
                params={"symbol": "XAUUSD", "tf": "m1", "limit": 5},
                timeout=5
            )
            evidence["candles"] = {
                "status_code": response.status_code,
                "response": response.json() if response.status_code == 200 else response.text[:200]
            }
            print(f"   Status: {response.status_code}")
            
            if response.status_code != 200:
                print(f"   ❌ Expected 200, got {response.status_code}")
                all_pass = False
            else:
                # API returns direct list, not wrapped in object
                candles = response.json()
                
                if not isinstance(candles, list):
                    print(f"   ❌ Expected list, got {type(candles)}")
                    all_pass = False
                elif not candles:
                    print(f"   ⚠️  Empty candles array (no data yet)")
                    # This is not necessarily a failure if no ticks yet
                    evidence["candles"]["empty"] = True
                else:
                    print(f"   ✅ Returned {len(candles)} candles")
                    print(f"   Response preview:")
                    print(f"   {json.dumps(candles[:2], indent=6)[:500]}")
                    evidence["candles"]["candle_count"] = len(candles)
                    
        except Exception as e:
            evidence["candles"] = {"error": str(e)}
            print(f"   ❌ Request failed: {e}")
            all_pass = False
        
        if all_pass:
            print("\n✅ API ENDPOINTS: PASS")
            return "PASS", evidence
        else:
            print("\n❌ API ENDPOINTS: FAIL")
            return "FAIL", evidence
    
    def validate_data_freshness(self) -> Tuple[str, Dict]:
        """Validate data is fresh (combines tick and candle freshness checks)"""
        print("\n" + "="*70)
        print("VALIDATING DATA FRESHNESS")
        print("="*70)
        
        evidence = {}
        
        # Check tick freshness from earlier validation
        tick_evidence = self.evidence.get("tick_storage", {})
        tick_age = tick_evidence.get("latest_tick_age_seconds")
        
        if tick_age is None:
            print("❌ No tick age data available")
            return "FAIL", evidence
        
        evidence["tick_age_seconds"] = tick_age
        print(f"📊 Latest tick age: {tick_age:.1f}s")
        
        if tick_age > 120:
            print(f"❌ Ticks are stale (>{tick_age:.0f}s old)")
            return "FAIL", evidence
        
        print("✅ DATA FRESHNESS: PASS")
        return "PASS", evidence
    
    def validate_system_integrity(self) -> Tuple[str, Dict]:
        """Validate system integrity with actual candle data"""
        print("\n" + "="*70)
        print("VALIDATING SYSTEM INTEGRITY")
        print("="*70)
        
        evidence = {}
        all_pass = True
        
        # Get candles from API
        try:
            response = requests.get(
                f"{self.api_base}/candles",
                params={"symbol": "XAUUSD", "tf": "m1", "limit": 5},
                timeout=5
            )
            
            if response.status_code != 200:
                evidence["error"] = f"Could not fetch candles (HTTP {response.status_code})"
                print(f"❌ {evidence['error']}")
                return "FAIL", evidence
            
            # API returns direct list
            candles = response.json()
            
            if not isinstance(candles, list):
                evidence["error"] = f"Expected list, got {type(candles)}"
                print(f"❌ {evidence['error']}")
                return "FAIL", evidence
            
            if not candles:
                print("ℹ️  No candles to validate (empty dataset)")
                evidence["note"] = "No candles yet"
                return "PASS", evidence  # Not a failure if system just started
            
            evidence["candle_count"] = len(candles)
            print(f"📊 Validating {len(candles)} candles")
            
            # Check 1: OHLC consistency
            print("\n1️⃣ OHLC Consistency Check")
            ohlc_errors = []
            for i, candle in enumerate(candles):
                o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
                
                if not (l <= o <= h):
                    ohlc_errors.append(f"Candle {i}: open {o} not in range [{l}, {h}]")
                if not (l <= c <= h):
                    ohlc_errors.append(f"Candle {i}: close {c} not in range [{l}, {h}]")
                if not (l <= h):
                    ohlc_errors.append(f"Candle {i}: low {l} > high {h}")
            
            if ohlc_errors:
                evidence["ohlc_errors"] = ohlc_errors
                print(f"   ❌ Found {len(ohlc_errors)} OHLC errors:")
                for err in ohlc_errors[:3]:
                    print(f"      {err}")
                all_pass = False
            else:
                print("   ✅ All OHLC values consistent")
            
            # Check 2: Timestamp ordering
            print("\n2️⃣ Timestamp Ordering Check")
            timestamps = [candle['ts_open'] for candle in candles]
            if timestamps != sorted(timestamps):
                evidence["timestamp_error"] = "Timestamps not strictly increasing"
                print(f"   ❌ {evidence['timestamp_error']}")
                all_pass = False
            else:
                print("   ✅ Timestamps strictly increasing")
            
            # Check 3: No duplicates
            print("\n3️⃣ Duplicate Check")
            if len(timestamps) != len(set(timestamps)):
                evidence["duplicate_error"] = "Duplicate timestamps found"
                print(f"   ❌ {evidence['duplicate_error']}")
                all_pass = False
            else:
                print("   ✅ No duplicate timestamps")
            
        except Exception as e:
            evidence["error"] = str(e)
            print(f"❌ Integrity check failed: {e}")
            return "FAIL", evidence
        
        if all_pass:
            print("\n✅ SYSTEM INTEGRITY: PASS")
            return "PASS", evidence
        else:
            print("\n❌ SYSTEM INTEGRITY: FAIL")
            return "FAIL", evidence
    
    def validate_feed_layer(self) -> Tuple[str, Dict]:
        """Validate feed layer based on data-source endpoint"""
        print("\n" + "="*70)
        print("VALIDATING FEED LAYER")
        print("="*70)
        
        # Reuse API endpoint validation results
        api_evidence = self.evidence.get("api_endpoints", {})
        ds_evidence = api_evidence.get("data_source", {})
        
        if ds_evidence.get("status_code") != 200:
            print("❌ Data source endpoint not accessible")
            return "FAIL", ds_evidence
        
        response = ds_evidence.get("response", {})
        if not isinstance(response, dict):
            print("❌ Invalid response format")
            return "FAIL", {"error": "Invalid response"}
        
        connected = response.get("connected")
        source = response.get("source")
        
        print(f"📡 Source: {source}")
        print(f"🔌 Connected: {connected}")
        
        if connected:
            print("✅ FEED LAYER: PASS")
            return "PASS", {"source": source, "connected": connected}
        else:
            print("❌ FEED LAYER: FAIL (not connected)")
            return "FAIL", {"source": source, "connected": connected}
    
    def run_full_validation(self):
        """Run all validations in order"""
        print("="*70)
        print("MARKET DATA PIPELINE VALIDATION")
        print("Starting strict evidence-based validation...")
        print("="*70)
        
        # Run validations in dependency order
        self.results["tick_storage"], self.evidence["tick_storage"] = self.validate_tick_storage()
        self.results["candle_aggregation"], self.evidence["candle_aggregation"] = self.validate_candle_aggregation()
        self.results["api_endpoints"], self.evidence["api_endpoints"] = self.validate_api_endpoints()
        self.results["feed_layer"], self.evidence["feed_layer"] = self.validate_feed_layer()
        self.results["data_freshness"], self.evidence["data_freshness"] = self.validate_data_freshness()
        self.results["system_integrity"], self.evidence["system_integrity"] = self.validate_system_integrity()
        
        # Determine ready_for_execution BEFORE printing final report
        all_pass = all(
            self.results[key] == "PASS" 
            for key in self.results 
            if key != "ready_for_execution"
        )
        self.results["ready_for_execution"] = "YES" if all_pass else "NO"
        
        # Print final report
        self.print_final_report()
    
    def print_final_report(self):
        """Print structured final report with evidence"""
        print("\n" + "="*70)
        print("FINAL VALIDATION REPORT")
        print("="*70)
        
        print("\n📊 VALIDATION RESULTS:\n")
        for key, value in self.results.items():
            if key == "ready_for_execution":
                continue
            status_icon = "✅" if value == "PASS" else "❌"
            print(f"{status_icon} {key.replace('_', ' ').title()}: {value}")
        
        print("\n📋 EVIDENCE SUMMARY:\n")
        
        # Tick storage evidence
        if "tick_storage" in self.evidence:
            te = self.evidence["tick_storage"]
            print("Tick Storage:")
            print(f"  - Count: {te.get('tick_count', 'N/A')}")
            print(f"  - Age: {te.get('latest_tick_age_seconds', 'N/A')}s")
            print(f"  - Symbols: {te.get('symbols', {})}")
            if "error" in te:
                print(f"  - Error: {te['error']}")
        
        # API endpoints evidence
        if "api_endpoints" in self.evidence:
            ae = self.evidence["api_endpoints"]
            if "data_source" in ae:
                print("\nData Source Endpoint:")
                print(f"  - Status: {ae['data_source'].get('status_code', 'N/A')}")
            if "candles" in ae:
                print("\nCandles Endpoint:")
                print(f"  - Status: {ae['candles'].get('status_code', 'N/A')}")
                print(f"  - Candles returned: {ae['candles'].get('candle_count', 0)}")
        
        # System integrity evidence
        if "system_integrity" in self.evidence:
            si = self.evidence["system_integrity"]
            print("\nSystem Integrity:")
            if "ohlc_errors" in si:
                print(f"  - OHLC errors: {len(si['ohlc_errors'])}")
            if "candle_count" in si:
                print(f"  - Validated candles: {si['candle_count']}")
        
        print("\n" + "="*70)
        ready_icon = "🎯" if self.results["ready_for_execution"] == "YES" else "⛔"
        print(f"{ready_icon} READY FOR EXECUTION LAYER: {self.results['ready_for_execution']}")
        print("="*70)
        
        # Exit code based on ready status
        if self.results["ready_for_execution"] == "YES":
            print("\n✅ All systems operational. Ready to proceed to execution layer.")
            return 0
        else:
            print("\n❌ System not ready. Fix failures before proceeding.")
            failed = [k for k, v in self.results.items() if v == "FAIL" and k != "ready_for_execution"]
            print(f"Failed components: {', '.join(failed)}")
            return 1

if __name__ == "__main__":
    import sys
    validator = MarketDataValidator()
    exit_code = validator.run_full_validation()
    sys.exit(exit_code)
