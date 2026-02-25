#!/bin/bash
set -e
cd /workspaces/BNK_TRADESYSTEM
rm -f data/trading.db
BNK_DEMO_ENGINE=1 uvicorn app.api.server:create_api_app --factory --host 0.0.0.0 --port 9000 --log-level warning &
UVICORN_PID=$!
echo "uvicorn PID: $UVICORN_PID"
sleep 10
echo "--- /api/v1/status ---"
curl -s http://localhost:9000/api/v1/status | python -m json.tool
echo ""
echo "--- /api/v1/account ---"
curl -s http://localhost:9000/api/v1/account | python -m json.tool
echo ""
echo "--- /api/v1/signals/recent?limit=5 ---"
curl -s "http://localhost:9000/api/v1/signals/recent?limit=5" | python -m json.tool
kill $UVICORN_PID 2>/dev/null
echo "uvicorn killed"
