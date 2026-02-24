# BNK TradeSystem — Gold/Silver Trading Assistant

A production-grade **Gold/Silver Trading Assistant + Auto-Trading System** for XAUUSD and XAGUSD, operated via Telegram with 24/7 automated analysis, risk governance, and optional paper/live execution.

---

## Features

- **Three Modes**: ASSIST (alerts only), PAPER (simulate trades), LIVE (cTrader execution)
- **Full Telegram Interface**: Commands, inline confirm/reject/snooze buttons, daily reports
- **Rule-based Strategy**: EMA200 bias on 1H + EMA20/50 pullback + RSI cross + candle breakout on 15m
- **Robust Risk Governor**: Daily trade/loss limits, drawdown cap, cooldowns, kill switch
- **Setup Scorer**: 0–10 quality score with reason breakdown
- **SQLite Storage**: Signals, trades, daily state with async access
- **FastAPI REST API**: Optional dashboard/OpenClaw integration endpoints
- **APScheduler**: Analysis + trade-manager runs every 60 seconds
- **Docker-ready**: `docker-compose.yml` and `Dockerfile` included

---

## Architecture

```
app/
  main.py              # Entrypoint — wires everything together
  config.py            # All settings from .env
  logging_config.py    # Loguru setup

  domain/
    enums.py           # Mode, Side, Symbol, Bias, etc.
    models.py          # TradeIdea, TradeResult, RiskState, MarketContext
    errors.py          # Custom exceptions

  data/
    market_data.py     # Provider interface + facade
    providers/
      ohlc_csv.py      # CSV + SyntheticDataProvider (dev/testing)
      ctrader_data.py  # cTrader live data (stub — implement when ready)
    storage.py         # Async SQLite CRUD
    migrations.sql     # DB schema

  strategy/
    features.py        # EMA, RSI, ATR indicator helpers
    regime.py          # Volatility regime detection
    rules.py           # Full strategy rule evaluation
    scorer.py          # Setup quality scoring (0-10)
    risk.py            # SL/TP calculation + position sizing

  execution/
    base.py            # Abstract Executor interface
    paper.py           # Paper trading executor
    ctrader.py         # Live cTrader executor (stub)
    safeguards.py      # Pre-execution safety checks

  services/
    analyzer.py        # Orchestrates fetch → strategy → risk → notify
    trade_manager.py   # Monitors open trades, marks to market
    locks.py           # Risk governor (max trades, cooldowns, kill switch)
    news_filter.py     # News event filter (stub — add real feed)
    scheduler.py       # APScheduler jobs

  telegram/
    bot.py             # Application factory + signal notify + callbacks
    commands.py        # All command handler functions
    keyboards.py       # Inline button layouts
    formatters.py      # MarkdownV2 message formatters
    auth.py            # Admin allow-list + PIN verification

  api/
    server.py          # FastAPI app factory
    routes.py          # REST endpoints
```

---

## Quick Start

### 1. Clone & install

```bash
git clone <repo-url>
cd BNK_TRADESYSTEM
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — fill in TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_IDS at minimum
```

### 3. Run

```bash
python -m app.main
```

### 4. Docker

```bash
docker compose up -d
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | _required_ | Bot token from @BotFather |
| `TELEGRAM_ADMIN_CHAT_IDS` | _required_ | Comma-separated admin chat IDs |
| `TELEGRAM_PIN` | `0000` | PIN for admin commands |
| `MODE` | `assist` | `assist` / `paper` / `live` |
| `RISK_PER_TRADE_PCT` | `0.5` | Risk per trade as % of balance |
| `MAX_TRADES_PER_DAY` | `1` | Max trades per day |
| `MAX_LOSSES_PER_DAY` | `1` | Max losses per day |
| `DAILY_DD_CAP_PCT` | `2.0` | Daily drawdown cap % |
| `COOLDOWN_MIN_AFTER_LOSS` | `180` | Minutes to wait after a loss |
| `SYMBOLS` | `XAUUSD,XAGUSD` | Active symbols |

See `.env.example` for the full list.

---

## Telegram Commands

### Core
| Command | Description |
|---|---|
| `/start` | Help + current mode |
| `/help` | Full command list |
| `/status` | Mode, PnL, locks, drawdown |
| `/analyze` | Force analysis scan now |

### Analysis
| Command | Description |
|---|---|
| `/signals` | Last 10 signals with scores |
| `/bias` | 1H directional bias for XAU/XAG |
| `/levels` | Daily high/low + prior session levels |

### Mode Control (admin)
| Command | Description |
|---|---|
| `/mode assist\|paper\|live` | Switch mode (live requires PIN) |
| `/live_on` / `/live_off` | Toggle live trading |
| `/paper_on` / `/paper_off` | Toggle paper trading |

### Risk Controls (admin)
| Command | Description |
|---|---|
| `/risk` | Show risk settings |
| `/risk set pct 0.5` | Set risk per trade % |
| `/limits` | Show daily limits |
| `/limits set max_losses 1` | Set max losses |
| `/limits set daily_dd 2.0` | Set drawdown cap |
| `/cooldown 120` | Set cooldown minutes |

### Safety (admin)
| Command | Description |
|---|---|
| `/pause <minutes>` | Pause trading |
| `/resume` | Resume trading |
| `/kill` | 🚨 Emergency kill switch |
| `/unlock <PIN>` | Clear all locks |

---

## Strategy Logic

**Bias (1H timeframe):**
- Bullish if `Close(1H) > EMA200(1H)`
- Bearish if `Close(1H) < EMA200(1H)`

**Entry (15m timeframe):**
1. Price near EMA20 or EMA50 (pullback within 0.3%)
2. RSI crossed above 50 (buy) or below 50 (sell) in last 3 bars
3. Latest candle close broke prior candle's high (buy) or low (sell)

**SL/TP:**
- SL = `1.2 × ATR(14)` from entry
- TP = `1.8 × risk` (RR 1:1.8)

**Filters:**
- Spread exceeds maximum threshold
- ATR spike (extreme volatility)
- Session: London (07:00–16:00 UTC) + NY (13:00–21:00 UTC)
- News window (manual events or future calendar feed)

---

## Risk Governor Rules

1. Max 1 trade per day
2. Max 1 loss per day → 3-hour cooldown
3. 2% daily drawdown cap
4. Kill switch (immediate halt)
5. Manual pause for N minutes

---

## REST API (optional)

When running, the FastAPI server is available at `http://localhost:8000`.

| Endpoint | Description |
|---|---|
| `GET /api/v1/health` | Health check |
| `GET /api/v1/status` | System status |
| `GET /api/v1/signals/recent` | Recent signals |
| `GET /api/v1/report/today` | Today's trade summary |
| `GET /api/v1/mode` | Current mode |
| `POST /api/v1/mode/{name}` | Set mode |

---

## Development

```bash
# Run tests
pytest tests/ -v

# Lint
ruff check app/ tests/

# Type check
mypy app/
```

### Switching to a real data provider

In `app/main.py`, replace `SyntheticDataProvider` with:

```python
from app.data.providers.ohlc_csv import CSVDataProvider
provider = CSVDataProvider("data/csv")  # place XAUUSD_15m.csv etc. in data/csv/

# or later, once implemented:
from app.data.providers.ctrader_data import CTraderDataProvider
provider = CTraderDataProvider(
    client_id=settings.ctrader_client_id,
    client_secret=settings.ctrader_client_secret,
    access_token=settings.ctrader_access_token,
    account_id=settings.ctrader_account_id,
)
```

---

## Roadmap

- [x] Core system scaffold + config + logging
- [x] Domain models + enums + errors
- [x] Synthetic + CSV market data providers
- [x] Strategy: EMA bias + pullback + RSI + candle breakout
- [x] Risk governor + locks (max trades, cooldown, kill switch)
- [x] SQLite storage (signals / trades / state)
- [x] Paper executor (simulated trade outcomes)
- [x] APScheduler (analysis + trade-manager every 60s)
- [x] Telegram bot: all commands + inline confirm/reject/snooze
- [x] FastAPI REST API
- [x] Test suite (37 tests)
- [ ] cTrader live data provider (implement WebSocket)
- [ ] cTrader live execution adapter
- [ ] Real news filter (economic calendar API)
- [ ] Trailing stop / partial close
- [ ] Multi-timeframe confluence scoring
- [ ] Back-test runner on CSV data

---

## Disclaimer

This software is for educational and personal use only. Trading financial instruments involves significant risk. Past performance is not indicative of future results. Always test thoroughly in PAPER mode before enabling LIVE mode.
