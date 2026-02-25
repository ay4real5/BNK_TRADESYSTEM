#!/usr/bin/env python3
"""
cTrader OAuth Integration Test Script

Tests the OAuth flow and connection without placing any trades.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.ctrader_oauth import oauth_service
from app.config import settings
from loguru import logger


async def test_oauth_setup() -> dict[str, str]:
    """Test the OAuth setup and configuration."""
    results = {
        "OAuth flow": "FAIL",
        "Token storage": "FAIL",
        "Token refresh": "FAIL",
        "Account discovery": "FAIL",
        "Status endpoint": "FAIL",
        "Ready for demo execution integration": "NO",
    }

    print("\n" + "=" * 70)
    print("cTrader OAuth Integration Test")
    print("=" * 70)

    # Check configuration
    print("\n[1/5] Checking Configuration...")
    if not settings.ctrader_client_id:
        print("  ❌ CTRADER_CLIENT_ID not set in .env")
        return results
    if not settings.ctrader_client_secret:
        print("  ❌ CTRADER_CLIENT_SECRET not set in .env")
        return results
    if settings.ctrader_env not in ["demo", "live"]:
        print(f"  ❌ Invalid CTRADER_ENV: {settings.ctrader_env} (must be 'demo' or 'live')")
        return results

    print(f"  ✅ Client ID: {settings.ctrader_client_id[:8]}...")
    print(f"  ✅ Environment: {settings.ctrader_env}")
    print(f"  ✅ Redirect URI: {settings.ctrader_redirect_uri}")

    # Check if tokens are already stored
    print("\n[2/5] Checking Token Storage...")
    try:
        access_token = await oauth_service._get_secret("ctrader_access_token")
        refresh_token = await oauth_service._get_secret("ctrader_refresh_token")
        expires_at = await oauth_service._get_secret("ctrader_token_expires_at")

        if access_token and refresh_token:
            print(f"  ✅ Access token found (expires: {expires_at})")
            print(f"  ✅ Refresh token found")
            results["Token storage"] = "PASS"
        else:
            print("  ⚠️  No tokens found. Please complete OAuth flow first:")
            print(f"     Visit: http://localhost:8000/auth/ctrader/login")
            results["OAuth flow"] = "PENDING"
            return results
    except Exception as e:
        print(f"  ❌ Error checking tokens: {e}")
        return results

    results["OAuth flow"] = "PASS"

    # Test token refresh
    print("\n[3/5] Testing Token Refresh...")
    try:
        new_token = await oauth_service.get_valid_access_token()
        if new_token:
            print(f"  ✅ Token refresh successful")
            print(f"     Token: {new_token[:16]}...")
            results["Token refresh"] = "PASS"
        else:
            print("  ❌ Token refresh returned empty token")
    except Exception as e:
        print(f"  ❌ Token refresh failed: {e}")

    # Test account discovery
    print("\n[4/5] Testing Account Discovery...")
    try:
        accounts = await oauth_service.discover_accounts()
        if accounts:
            print(f"  ✅ Discovered {len(accounts)} account(s)")
            for acc in accounts:
                acc_type = "DEMO" if not acc.get("isLive") else "LIVE"
                acc_id = acc.get("ctidTraderAccountId", "Unknown")
                currency = acc.get("depositCurrency", "Unknown")
                print(f"     - ID: {acc_id} | Type: {acc_type} | Currency: {currency}")
            results["Account discovery"] = "PASS"
        else:
            print("  ⚠️  No accounts found")
    except Exception as e:
        print(f"  ❌ Account discovery failed: {e}")

    # Test status endpoint
    print("\n[5/5] Testing Connection Status...")
    try:
        status = await oauth_service.test_connection()
        if status.get("connected"):
            print(f"  ✅ Connection test successful")
            print(f"     Connected: {status['connected']}")
            print(f"     Token Valid: {status['token_valid']}")
            print(f"     Environment: {status['environment']}")
            print(f"     Account ID: {status.get('account_id', 'Not set')}")
            print(f"     Last Heartbeat: {status['last_heartbeat']}")
            results["Status endpoint"] = "PASS"
            results["Ready for demo execution integration"] = "YES"
        else:
            print(f"  ❌ Connection failed")
            if "error" in status:
                print(f"     Error: {status['error']}")
    except Exception as e:
        print(f"  ❌ Status endpoint failed: {e}")

    return results


async def display_status_json():
    """Display the status endpoint JSON response."""
    print("\n" + "=" * 70)
    print("cTrader Status Endpoint Response (Secrets Redacted)")
    print("=" * 70)

    try:
        status = await oauth_service.test_connection()

        # Redact sensitive information
        if "token_expires_at" in status and status["token_expires_at"]:
            # Only show date portion
            status["token_expires_at"] = status["token_expires_at"][:19] + " (redacted)"

        import json
        print(json.dumps(status, indent=2))
    except Exception as e:
        print(f"Error fetching status: {e}")


async def main():
    """Run the test suite."""
    try:
        # Initialize database
        from app.data.storage import init_db
        await init_db()

        # Run tests
        results = await test_oauth_setup()

        # Display results
        print("\n" + "=" * 70)
        print("Test Results Summary")
        print("=" * 70)
        for test_name, result in results.items():
            status_icon = "✅" if result == "PASS" or result == "YES" else "❌"
            print(f"{status_icon} {test_name}: {result}")

        # Display status JSON if connected
        if results.get("Status endpoint") == "PASS":
            await display_status_json()

        print("\n" + "=" * 70)
        print("Next Steps:")
        print("=" * 70)
        if results["OAuth flow"] == "PENDING":
            print("1. Start the backend server: uvicorn app.main:app --reload")
            print("2. Visit: http://localhost:8000/auth/ctrader/login")
            print("3. Complete the OAuth flow")
            print("4. Re-run this test script")
        elif results["Ready for demo execution integration"] == "YES":
            print("✅ OAuth setup complete!")
            print("✅ Ready to integrate cTraderExecutionService behind MODE=demo")
            print()
            print("No trades have been placed. Demo trading integration can now proceed.")
        else:
            print("⚠️  Some tests failed. Review the errors above.")

        print("=" * 70 + "\n")

    except Exception as e:
        logger.error(f"Test suite failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
