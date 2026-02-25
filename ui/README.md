# BNK TradeSystem — Operator Dashboard

Single-page React dashboard for real-time monitoring and control of the
BNK_TRADESYSTEM trading bot.

## Stack

| Layer    | Tech        |
|----------|-------------|
| UI       | React 18 + Vite 6 |
| Charts   | Recharts    |
| Styling  | Plain CSS (no frameworks) |
| API      | Fetch → FastAPI backend |
| Refresh  | 5-second polling |

## Panels

| #  | Panel             | Endpoint              |
|----|-------------------|-----------------------|
| 1  | Account State     | GET /api/v1/account   |
| 2  | System Status     | GET /api/v1/status    |
| 3  | Recent Signals    | GET /api/v1/signals/recent?limit=20 |
| 4  | Today's Report    | GET /api/v1/report/today |
| 5  | Mode Controls     | GET+POST /api/v1/mode |
| +  | Equity Curve      | Derived from signals  |

---

## Running locally (same machine as API)

```bash
# Terminal 1 — start the backend
cd /workspaces/BNK_TRADESYSTEM
BNK_DEMO_ENGINE=1 uvicorn app.api.server:create_api_app --factory --reload --host 0.0.0.0 --port 8000

# Terminal 2 — start the UI dev server
cd /workspaces/BNK_TRADESYSTEM/ui
npm install
npm run dev
```

Open **http://localhost:5173** in your browser.

The Vite dev server proxies all `/api/*` requests to `http://localhost:8000`
automatically — no CORS issues.

---

## Running in GitHub Codespaces

1. Start the backend on port 8000 (same command as above).
2. **Forward port 8000**: VS Code will prompt, or go to Ports panel → add 8000.
3. Copy the public forwarded URL, e.g.
   `https://fuzzy-garbanzo-xxxx-8000.app.github.dev`
4. Start the UI:
   ```bash
   cd /workspaces/BNK_TRADESYSTEM/ui
   npm install
   npm run dev
   ```
5. Forward port 5173 as well. Open the 5173 URL.

**If the UI can't reach the API** (e.g. different origin), open the browser
console on the dashboard page and run:

```js
localStorage.setItem('bnk_api_url', 'https://YOUR-CODESPACE-8000.app.github.dev')
location.reload()
```

The dashboard will then call the API at that URL directly.

---

## Pointing at a remote / production API

Set `VITE_API_URL` in a `.env` file inside `ui/`:

```
# ui/.env
VITE_API_URL=https://your-api-host.example.com
```

Then rebuild:
```bash
npm run build
```

Or set it at runtime via `localStorage` as shown above.

---

## Building for production

```bash
cd ui
npm run build
# Static files are output to ui/dist/
# Serve with: npx serve dist -p 5173
```

---

## Auto-refresh

All panels poll their respective endpoints every **5 seconds**.
A countdown badge (↻ Ns) is shown on the Signals panel.
The live/dead indicator in the header pings `/api/v1/health` every 10 seconds.
