"""
cTrader OAuth 2.0 authentication and token management.

Handles:
- OAuth2 authorization flow
- Token storage and retrieval
- Token refresh logic
- Account discovery
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import aiosqlite
import httpx
from loguru import logger

from ..config import settings


DB_PATH = "data/trading.db"


class CTraderOAuthService:
    """Manages cTrader OAuth2 flow and token lifecycle."""

    # cTrader OAuth endpoints (Open API only - no separate demo/live hosts)
    DEMO_AUTH_URL = "https://openapi.ctrader.com/apps/auth"
    DEMO_TOKEN_URL = "https://openapi.ctrader.com/apps/token"
    DEMO_API_URL = "https://openapi.ctrader.com/v2"  # FIX: was /connect, correct is /v2

    LIVE_AUTH_URL = "https://openapi.ctrader.com/apps/auth"
    LIVE_TOKEN_URL = "https://openapi.ctrader.com/apps/token"
    LIVE_API_URL = "https://openapi.ctrader.com/v2"  # FIX: was /connect, correct is /v2

    # OAuth scopes (space-delimited) required for account discovery and trading
    # "trading" = full trading access (includes account read permission)
    OAUTH_SCOPE = "trading"

    def __init__(self) -> None:
        self.is_demo = settings.ctrader_env == "demo"
        self.auth_url = self.DEMO_AUTH_URL if self.is_demo else self.LIVE_AUTH_URL
        self.token_url = self.DEMO_TOKEN_URL if self.is_demo else self.LIVE_TOKEN_URL
        self.api_url = self.DEMO_API_URL if self.is_demo else self.LIVE_API_URL
        
        # Validate URLs at startup
        self._validate_urls()
        logger.info(
            "cTrader OAuth initialized | env={} | auth_url={} | token_url={} | api_url={}",
            settings.ctrader_env,
            self.auth_url,
            self.token_url,
            self.api_url
        )
    
    def _validate_urls(self) -> None:
        """Validate that all cTrader URLs are properly configured."""
        for url_name, url in [("auth_url", self.auth_url), ("token_url", self.token_url), ("api_url", self.api_url)]:
            if not url.startswith("https://"):
                raise ValueError(f"cTrader {url_name} must use HTTPS: {url}")
            if ".ctrader.com" not in url:
                raise ValueError(f"cTrader {url_name} must use .ctrader.com domain: {url}")

    def get_authorization_url(self) -> str:
        """Generate the OAuth authorization URL for user login."""
        # Generate random state for CSRF protection
        state = secrets.token_urlsafe(32)
        
        params = {
            "client_id": settings.ctrader_client_id,
            "redirect_uri": settings.ctrader_redirect_uri,
            "response_type": "code",
            "scope": self.OAUTH_SCOPE,
            "state": state,
        }
        
        url = f"{self.auth_url}?{urlencode(params)}"
        
        logger.info(
            "Generated OAuth URL | auth_url={} | client_id={}... | redirect_uri={} | state={}...",
            self.auth_url,
            settings.ctrader_client_id[:20],
            settings.ctrader_redirect_uri,
            state[:10]
        )
        
        return url

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        """
        Exchange authorization code for access and refresh tokens.
        
        Returns:
            Token data including access_token, refresh_token, expires_in
        """
        request_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.ctrader_redirect_uri,
            "client_id": settings.ctrader_client_id,
            "client_secret": settings.ctrader_client_secret,
        }
        
        logger.info(
            "🔑 TOKEN EXCHANGE REQUEST | url={} | client_id={}... | redirect_uri={}",
            self.token_url,
            settings.ctrader_client_id[:30],
            settings.ctrader_redirect_uri
        )
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.token_url,
                    data=request_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                
                # Log response details
                logger.info(
                    "📥 TOKEN RESPONSE | status={} | content_type={} | size={}",
                    response.status_code,
                    response.headers.get("content-type", "unknown"),
                    len(response.content)
                )
                
                # Always log the response body for debugging (sanitized)
                response_text = response.text
                # Don't log full response if it contains tokens
                try:
                    resp_json = response.json()
                    if "accessToken" in resp_json or "access_token" in resp_json:
                        logger.info("📄 TOKEN RESPONSE | status=200 | contains_access_token=true | keys={}", list(resp_json.keys()))
                        response_preview = f"{{...tokens received, keys: {list(resp_json.keys())}}}"
                    else:
                        response_preview = response_text[:500]
                        logger.info("📄 RESPONSE BODY: {}", response_preview)
                except:
                    response_preview = response_text[:500]
                    logger.info("📄 RESPONSE BODY: {}", response_preview)
                
                # Prepare debug information (sanitized - no token values)
                debug_info = {
                    "token_url": self.token_url,
                    "client_id": settings.ctrader_client_id[:30] + "...",
                    "redirect_uri": settings.ctrader_redirect_uri,
                    "grant_type": "authorization_code",
                    "code_length": len(code),
                    "response_status": response.status_code,
                    "response_content_type": response.headers.get("content-type", "unknown"),
                    "response_body": response_preview,
                }
                
                # Write detailed debug info to file
                with open("/workspaces/BNK_TRADESYSTEM/oauth_debug.txt", "w") as f:
                    f.write("=" * 80 + "\n")
                    f.write("cTrader OAuth Token Exchange Debug\n")
                    f.write("=" * 80 + "\n\n")
                    for key, value in debug_info.items():
                        f.write(f"{key}: {value}\n")
                
                if response.status_code != 200:
                    logger.error(
                        "❌ TOKEN EXCHANGE HTTP ERROR | status={} | body={}",
                        response.status_code,
                        response_text
                    )
                    raise ValueError(f"HTTP {response.status_code}: {response_text[:200]}")
                
                try:
                    token_data = response.json()
                except Exception as json_err:
                    logger.error("❌ Failed to parse JSON response: {}", json_err)
                    raise ValueError(f"Invalid JSON response: {response_text[:200]}")
                
                logger.info("📋 TOKEN RESPONSE KEYS: {}", list(token_data.keys()))
                
                # Check for error in response (cTrader uses 'error' or 'errorCode')
                # Only treat as error if errorCode/error has a truthy value
                error_code = token_data.get("errorCode") or token_data.get("error")
                if error_code:
                    error_desc = token_data.get("description") or token_data.get("error_description") or "Unknown error"
                    logger.error("❌ CTRADER ERROR: code={} | description={}", error_code, error_desc)
                    
                    # Create detailed error with debug info attached
                    error = ValueError(f"{error_code}: {error_desc}")
                    error.__notes__ = [debug_info]
                    raise error
                
                # cTrader uses camelCase (accessToken), handle both formats
                access_token = token_data.get("access_token") or token_data.get("accessToken")
                refresh_token = token_data.get("refresh_token") or token_data.get("refreshToken")
                expires_in = token_data.get("expires_in") or token_data.get("expiresIn", 3600)
                token_scope = token_data.get("scope") or token_data.get("scopes") or ""
                
                if not access_token:
                    logger.error("❌ MISSING access_token | Response keys: {}", list(token_data.keys()))
                    debug_info["response_keys"] = list(token_data.keys())
                    error = ValueError(f"Missing access_token/accessToken. Keys: {list(token_data.keys())}")
                    error.__notes__ = [debug_info]
                    raise error

                # Calculate expiry timestamp
                expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

                # Store tokens in database (DO NOT LOG TOKEN VALUES)
                await self._store_tokens(
                    access_token=access_token,
                    refresh_token=refresh_token or "",
                    expires_at=expires_at,
                    token_scope=token_scope or self.OAUTH_SCOPE,
                )

                logger.info("✅ TOKENS STORED | expires_in={}s | has_refresh={}", expires_in, bool(refresh_token))
                
                # Return normalized token data (snake_case for consistency)
                return {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_in": expires_in,
                    "expires_at": expires_at,
                    "scope": token_scope or self.OAUTH_SCOPE,
                }
                
        except Exception as e:
            logger.exception("💥 TOKEN EXCHANGE FAILED: {}", str(e))
            raise

    async def refresh_access_token(self) -> dict[str, Any]:
        """
        Refresh the access token using the stored refresh token.
        
        Returns:
            New token data
        """
        refresh_token = await self._get_secret("ctrader_refresh_token")
        if not refresh_token:
            raise ValueError("No refresh token available")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": settings.ctrader_client_id,
                    "client_secret": settings.ctrader_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            token_data = response.json()

        # cTrader uses camelCase, handle both formats
        access_token = token_data.get("access_token") or token_data.get("accessToken")
        new_refresh_token = token_data.get("refresh_token") or token_data.get("refreshToken")
        expires_in = token_data.get("expires_in") or token_data.get("expiresIn", 3600)
        token_scope = token_data.get("scope") or token_data.get("scopes") or None
        
        if not access_token:
            raise ValueError(f"Token refresh failed: missing access_token. Keys: {list(token_data.keys())}")

        # Calculate expiry timestamp
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

        # Store new tokens (use old refresh token if new one not provided)
        await self._store_tokens(
            access_token=access_token,
            refresh_token=new_refresh_token or refresh_token,
            expires_at=expires_at,
            token_scope=token_scope,
        )

        logger.info("cTrader OAuth: Access token refreshed | expires_in={}s", expires_in)
        return {
            "access_token": access_token,
            "refresh_token": new_refresh_token or refresh_token,
            "expires_in": expires_in,
            "expires_at": expires_at,
            "scope": token_scope or self.OAUTH_SCOPE,
        }

    async def get_valid_access_token(self) -> str:
        """
        Get a valid access token, refreshing if necessary.
        
        Returns:
            Valid access token
        """
        access_token = await self._get_secret("ctrader_access_token")
        expires_at_str = await self._get_secret("ctrader_token_expires_at")

        # Check if token exists and is not expired
        if access_token and expires_at_str:
            try:
                expires_at = datetime.fromisoformat(expires_at_str)
                # Refresh if expiring within 5 minutes
                if datetime.utcnow() < expires_at - timedelta(minutes=5):
                    return access_token
            except ValueError:
                pass

        # Token expired or invalid, refresh it
        logger.info("Access token expired or missing, refreshing...")
        token_data = await self.refresh_access_token()
        return token_data["access_token"]

    async def discover_accounts(self) -> list[dict[str, Any]]:
        """
        Query cTrader API to discover available trading accounts.
        
        Uses Protobuf protocol over TCP/TLS (not HTTP REST).
        
        Returns:
            List of account objects with accountId, isLive, balance
            
        Raises:
            Exception: If Protobuf connection or auth fails
        """
        access_token = await self.get_valid_access_token()
        
        logger.info("🔍 Discovering cTrader accounts via Protobuf...")

        try:
            # Use Protobuf protocol for account discovery
            from ..integration.ctrader_trading import discover_accounts_protobuf
            
            accounts = await discover_accounts_protobuf(access_token)
            
            logger.success(f"✅ Discovered {len(accounts)} account(s)")
            
            # Store first demo account as default if not set
            if accounts and not await self._get_secret("ctrader_account_id"):
                # Find first demo account
                demo_account = next((acc for acc in accounts if not acc.get("isLive")), accounts[0])
                await self._store_account_id(str(demo_account.get("accountId", "")))
                logger.info(f"Stored default account ID: {demo_account.get('accountId')}")
            
            return accounts
            
        except Exception as e:
            logger.error("❌ Failed to discover accounts via Protobuf | error: {}", str(e))
            raise

    async def test_connection(self) -> dict[str, Any]:
        """
        Test the connection to cTrader API.
        
        Returns:
            Status information including connectivity, token validity
        """
        try:
            access_token = await self.get_valid_access_token()
            expires_at = await self._get_secret("ctrader_token_expires_at")
            account_id = await self._get_secret("ctrader_account_id")
            token_scope = await self._get_secret("ctrader_token_scope")
            
            # Calculate token expiry status
            token_expired = False
            seconds_remaining = None
            if expires_at:
                try:
                    expires_dt = datetime.fromisoformat(expires_at)
                    seconds_remaining = int((expires_dt - datetime.utcnow()).total_seconds())
                    token_expired = seconds_remaining < 0
                except:
                    pass

            # Try to fetch accounts (optional - don't fail if this errors)
            accounts_loaded = False
            accounts_error = None
            accounts_list = []
            try:
                accounts_data = await self.discover_accounts()
                accounts_loaded = True
                accounts_list = [
                    {
                        "id": str(acc.get("accountId", "")),
                        "type": "demo" if not acc.get("isLive") else "live",
                        "login": acc.get("traderLogin", ""),
                    }
                    for acc in accounts_data
                ]
            except Exception as acc_err:
                accounts_error = str(acc_err)
                logger.warning("Account discovery failed (non-fatal): {}", accounts_error)

            return {
                "connected": True,
                "token_valid": bool(access_token) and not token_expired,
                "token_expired": token_expired,
                "seconds_remaining": seconds_remaining,
                "environment": settings.ctrader_env,
                "account_id": account_id or "",
                "requested_scope": self.OAUTH_SCOPE,
                "token_scope": token_scope or "",
                "accounts_loaded": accounts_loaded,
                "accounts": accounts_list,
                "accounts_error": accounts_error,
                "last_heartbeat": datetime.utcnow().isoformat(),
                "token_expires_at": expires_at or "",
            }
        except Exception as e:
            logger.error(f"cTrader connection test failed: {e}")
            return {
                "connected": False,
                "token_valid": False,
                "environment": settings.ctrader_env,
                "account_id": "",
                "accounts": [],
                "last_heartbeat": datetime.utcnow().isoformat(),
                "error": str(e),
            }

    async def _store_tokens(
        self,
        access_token: str,
        refresh_token: str,
        expires_at: str,
        token_scope: str | None = None,
    ) -> None:
        """Store OAuth tokens in the database."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO secrets (key, value, updated_at) VALUES (?, ?, ?)",
                ("ctrader_access_token", access_token, now),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO secrets (key, value, updated_at) VALUES (?, ?, ?)",
                ("ctrader_refresh_token", refresh_token, now),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO secrets (key, value, updated_at) VALUES (?, ?, ?)",
                ("ctrader_token_expires_at", expires_at, now),
            )
            if token_scope is not None:
                await conn.execute(
                    "INSERT OR REPLACE INTO secrets (key, value, updated_at) VALUES (?, ?, ?)",
                    ("ctrader_token_scope", token_scope, now),
                )
            await conn.commit()

    async def _store_account_id(self, account_id: str) -> None:
        """Store the selected account ID."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO secrets (key, value, updated_at) VALUES (?, ?, ?)",
                ("ctrader_account_id", account_id, now),
            )
            await conn.commit()
        logger.info(f"Stored cTrader account ID: {account_id}")

    async def _get_secret(self, key: str) -> str | None:
        """Retrieve a secret from the database."""
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute("SELECT value FROM secrets WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else None


# Singleton instance
oauth_service = CTraderOAuthService()
