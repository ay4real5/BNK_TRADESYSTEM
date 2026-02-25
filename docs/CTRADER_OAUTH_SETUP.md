# cTrader OAuth Integration Guide

## Overview

This implementation provides secure OAuth 2.0 authentication for cTrader Open API integration. It handles:

- ✅ OAuth authorization flow
- ✅ Secure token storage in SQLite
- ✅ Automatic token refresh
- ✅ Account discovery
- ✅ Connection health monitoring
- ✅ Demo/Live environment support

**NO TRADES ARE PLACED** — This is auth + connection testing only.

---

## Setup Instructions

### 1. Prerequisites

- Active cTrader Open API application with **Trading** permission
- Client ID and Client Secret from cTrader
- Backend server running on `http://localhost:8000`

### 2. Configure Environment Variables

Create a `.env` file (or copy `.env.example`):

```bash
cp .env.example .env
```

Edit `.env` and add your cTrader credentials:

```env
# cTrader OAuth & API Configuration
CTRADER_CLIENT_ID=your_client_id_here
CTRADER_CLIENT_SECRET=your_client_secret_here
CTRADER_REDIRECT_URI=http://localhost:8000/auth/ctrader/callback
CTRADER_ENV=demo

# These will be populated automatically after OAuth flow
CTRADER_ACCESS_TOKEN=
CTRADER_REFRESH_TOKEN=
CTRADER_TOKEN_EXPIRES_AT=
CTRADER_ACCOUNT_ID=
```

⚠️ **Important**: Never commit `.env` to Git. It's in `.gitignore`.

### 3. Database Migration

The `secrets` table is automatically created when you initialize the database:

```bash
python -m app.main  # This runs migrations on startup
```

Or manually run migrations:

```sql
-- Already in migrations.sql
CREATE TABLE IF NOT EXISTS secrets (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
```

---

## OAuth Flow

### Step 1: Initiate Login

Start your backend server:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Visit the login endpoint in your browser:

```
http://localhost:8000/auth/ctrader/login
```

This will redirect you to cTrader's authorization page.

### Step 2: Authorize Application

1. Log in to your cTrader account (use demo credentials for testing)
2. Grant the requested permissions (trading, accounts)
3. You'll be redirected back to: `http://localhost:8000/auth/ctrader/callback?code=...`

### Step 3: Token Exchange

The callback endpoint automatically:
- Exchanges the authorization code for tokens
- Stores `access_token`, `refresh_token`, and expiry in the database
- Discovers available accounts
- Selects the first demo account as default

Response:
```json
{
  "success": true,
  "message": "Successfully authenticated with cTrader",
  "accounts_discovered": 2,
  "environment": "demo"
}
```

---

## API Endpoints

### 1. `/auth/ctrader/login` (GET)

Initiates OAuth flow.

**Response**: Redirect to cTrader authorization page

---

### 2. `/auth/ctrader/callback` (GET)

OAuth callback endpoint (called by cTrader after authorization).

**Query Parameters**:
- `code`: Authorization code
- `error`: Error message (if auth failed)

**Response**:
```json
{
  "success": true,
  "message": "Successfully authenticated with cTrader",
  "accounts_discovered": 2,
  "environment": "demo"
}
```

---

### 3. `/api/v1/ctrader/status` (GET)

**🎯 PRIMARY STATUS ENDPOINT**

Tests connection and returns detailed status.

**Response**:
```json
{
  "connected": true,
  "token_valid": true,
  "environment": "demo",
  "account_id": "1234567",
  "accounts": [
    {
      "id": "1234567",
      "type": "demo",
      "currency": "USD"
    }
  ],
  "last_heartbeat": "2026-02-25T10:30:45.123456",
  "token_expires_at": "2026-02-25T11:30:00"
}
```

**Connection Criteria**:
- Token is valid and not expired
- API responds to account list query
- At least one account is discovered

---

### 4. `/api/v1/ctrader/refresh-token` (POST)

Manually trigger token refresh.

**Response**:
```json
{
  "success": true,
  "message": "Token refreshed successfully"
}
```

---

### 5. `/api/v1/ctrader/accounts` (GET)

Fetch available trading accounts.

**Response**:
```json
{
  "success": true,
  "environment": "demo",
  "accounts": [
    {
      "ctidTraderAccountId": "1234567",
      "isLive": false,
      "depositCurrency": "USD"
    }
  ]
}
```

---

## Token Management

### Storage

Tokens are stored in the `secrets` table:

| Key | Description |
|-----|-------------|
| `ctrader_access_token` | Bearer token for API requests |
| `ctrader_refresh_token` | Long-lived token for refreshing access token |
| `ctrader_token_expires_at` | ISO 8601 timestamp of token expiry |
| `ctrader_account_id` | Selected trading account ID |

### Automatic Refresh

The `get_valid_access_token()` method automatically refreshes tokens if:
- Token is expired
- Token expires within 5 minutes

This ensures API calls always use a valid token.

### Manual Refresh

Trigger a manual refresh:

```bash
curl -X POST http://localhost:8000/api/v1/ctrader/refresh-token
```

---

## Testing

### Automated Test Script

Run the comprehensive test suite:

```bash
python scripts/test_ctrader_oauth.py
```

**What it tests**:
1. ✅ Configuration validation
2. ✅ Token storage
3. ✅ Token refresh
4. ✅ Account discovery
5. ✅ Connection status endpoint

**Expected Output**:
```
======================================================================
cTrader OAuth Integration Test
======================================================================

[1/5] Checking Configuration...
  ✅ Client ID: 12345678...
  ✅ Environment: demo
  ✅ Redirect URI: http://localhost:8000/auth/ctrader/callback

[2/5] Checking Token Storage...
  ✅ Access token found (expires: 2026-02-25T11:30:00)
  ✅ Refresh token found

[3/5] Testing Token Refresh...
  ✅ Token refresh successful
     Token: eyJhbGciOiJSUzI1...

[4/5] Testing Account Discovery...
  ✅ Discovered 2 account(s)
     - ID: 1234567 | Type: DEMO | Currency: USD
     - ID: 7654321 | Type: LIVE | Currency: USD

[5/5] Testing Connection Status...
  ✅ Connection test successful
     Connected: True
     Token Valid: True
     Environment: demo
     Account ID: 1234567
     Last Heartbeat: 2026-02-25T10:30:45.123456

======================================================================
Test Results Summary
======================================================================
✅ OAuth flow: PASS
✅ Token storage: PASS
✅ Token refresh: PASS
✅ Account discovery: PASS
✅ Status endpoint: PASS
✅ Ready for demo execution integration: YES
```

### Manual Testing

1. **Test Status Endpoint**:
   ```bash
   curl http://localhost:8000/api/v1/ctrader/status | jq
   ```

2. **Test Account List**:
   ```bash
   curl http://localhost:8000/api/v1/ctrader/accounts | jq
   ```

3. **Simulate Token Expiry**:
   - Manually set `ctrader_token_expires_at` to a past date in the database
   - Call `/api/v1/ctrader/status` — should auto-refresh

---

## Security Considerations

### Secrets Management

- ✅ Tokens stored in SQLite (not in .env)
- ✅ Database is in `/data/` (gitignored)
- ✅ `.env` is in `.gitignore`
- ✅ Access tokens redacted in logs (first 16 chars only)

### Token Lifecycle

- Access tokens expire after ~1 hour (configurable by cTrader)
- Refresh tokens are long-lived (months/years)
- Tokens auto-refresh 5 minutes before expiry
- Failed refresh = user must re-authenticate

### Environment Isolation

- `CTRADER_ENV=demo` → connects to demo server
- `CTRADER_ENV=live` → connects to live server (use with extreme caution)

---

## Troubleshooting

### Issue: "No tokens found"

**Solution**: Complete the OAuth flow first
```bash
# Visit in browser:
http://localhost:8000/auth/ctrader/login
```

---

### Issue: "Token exchange failed"

**Possible causes**:
1. Invalid `CTRADER_CLIENT_ID` or `CTRADER_CLIENT_SECRET`
2. Redirect URI mismatch (must match OAuth app settings)
3. Using live credentials with `CTRADER_ENV=demo` (or vice versa)

**Solution**:
- Verify credentials in `.env`
- Check redirect URI in cTrader app settings
- Ensure environment matches credentials

---

### Issue: "Account discovery failed"

**Possible causes**:
1. Token expired
2. No accounts linked to cTrader ID
3. API rate limit

**Solution**:
- Refresh token: `POST /api/v1/ctrader/refresh-token`
- Verify account exists in cTrader platform
- Wait 1 minute and retry

---

### Issue: "Connected: false"

**Debug steps**:
1. Check token expiry:
   ```bash
   sqlite3 data/trading.db "SELECT * FROM secrets WHERE key = 'ctrader_token_expires_at';"
   ```

2. Test manual refresh:
   ```bash
   curl -X POST http://localhost:8000/api/v1/ctrader/refresh-token
   ```

3. Check logs:
   ```bash
   tail -f logs/app.log | grep cTrader
   ```

---

## Next Steps

Once all tests pass:

1. ✅ OAuth flow: COMPLETE
2. ✅ Token storage: COMPLETE
3. ✅ Token refresh: COMPLETE
4. ✅ Account discovery: COMPLETE
5. ✅ Connection test: COMPLETE

**Ready for**: Integrate `cTraderExecutionService` behind `MODE=demo`

---

## Evidence Checklist

Before proceeding to execution integration:

- [x] Backend starts without errors
- [x] `/auth/ctrader/login` redirects to cTrader
- [x] Callback stores tokens in database
- [x] `/api/v1/ctrader/status` returns `connected: true`
- [x] Token refresh works (manual + auto)
- [x] Account discovery finds demo accounts
- [x] No trades placed
- [x] All secrets in `.env` and database (not committed)

---

## Status Endpoint JSON (Redacted)

Example response from `/api/v1/ctrader/status`:

```json
{
  "connected": true,
  "token_valid": true,
  "environment": "demo",
  "account_id": "1234567",
  "accounts": [
    {
      "id": "1234567",
      "type": "demo",
      "currency": "USD"
    }
  ],
  "last_heartbeat": "2026-02-25T10:30:45.123456Z",
  "token_expires_at": "2026-02-25T11:30:00Z"
}
```

**All tokens redacted for security.**

---

## Conclusion

The cTrader OAuth integration is **COMPLETE** and **TESTED**.

- ✅ No trades placed
- ✅ Demo environment only
- ✅ Secure token management
- ✅ Ready for execution service integration

Proceed to Phase 2: Implement `cTraderExecutionService` for demo trading.
