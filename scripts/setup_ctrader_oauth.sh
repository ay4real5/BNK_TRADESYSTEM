#!/bin/bash
# Quick Start Guide for cTrader OAuth Setup

set -e

echo "========================================================================"
echo "cTrader OAuth Integration — Quick Start"
echo "========================================================================"
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "❌ .env file not found!"
    echo ""
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo "✅ .env created"
    echo ""
    echo "⚠️  REQUIRED: Edit .env and add your cTrader credentials:"
    echo "   - CTRADER_CLIENT_ID=your_client_id"
    echo "   - CTRADER_CLIENT_SECRET=your_client_secret"
    echo ""
    echo "Then re-run this script."
    exit 1
fi

# Check if credentials are set
if ! grep -q "^CTRADER_CLIENT_ID=.\+" .env || ! grep -q "^CTRADER_CLIENT_SECRET=.\+" .env; then
    echo "❌ cTrader credentials not set in .env"
    echo ""
    echo "Please edit .env and add:"
    echo "   CTRADER_CLIENT_ID=your_client_id"
    echo "   CTRADER_CLIENT_SECRET=your_client_secret"
    echo ""
    exit 1
fi

echo "✅ Configuration found"
echo ""

# Initialize database
echo "[1/3] Initializing database..."
python -c "
import asyncio
from app.data.storage import init_db
asyncio.run(init_db())
print('✅ Database initialized')
"
echo ""

# Test OAuth service import
echo "[2/3] Testing OAuth service..."
python -c "from app.services.ctrader_oauth import oauth_service; print('✅ OAuth service loaded')"
echo ""

# Show next steps
echo "[3/3] Next Steps"
echo "========================================================================"
echo ""
echo "1. Start the backend server:"
echo "   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"
echo ""
echo "2. Complete OAuth flow:"
echo "   Visit: http://localhost:8000/auth/ctrader/login"
echo ""
echo "3. Test connection:"
echo "   curl http://localhost:8000/api/v1/ctrader/status | jq"
echo ""
echo "4. Run automated tests:"
echo "   python scripts/test_ctrader_oauth.py"
echo ""
echo "========================================================================"
echo "📖 Full documentation: docs/CTRADER_OAUTH_SETUP.md"
echo "========================================================================"
