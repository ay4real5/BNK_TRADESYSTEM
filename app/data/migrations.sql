-- Gold/Silver Trading System — SQLite schema
-- Run once during initialisation; idempotent via IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL,
    entry        REAL    NOT NULL,
    sl           REAL    NOT NULL,
    tp           REAL    NOT NULL,
    score        REAL    NOT NULL DEFAULT 0,
    reasons_json TEXT    NOT NULL DEFAULT '[]',
    mode         TEXT    NOT NULL DEFAULT 'assist',
    status       TEXT    NOT NULL DEFAULT 'pending',
    bias         TEXT    NOT NULL DEFAULT 'neutral'
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id    INTEGER REFERENCES signals(id),
    ts_open      TEXT    NOT NULL,
    ts_close     TEXT,
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL,
    entry        REAL    NOT NULL,
    sl           REAL    NOT NULL,
    tp           REAL    NOT NULL,
    size         REAL    NOT NULL DEFAULT 0,
    outcome      TEXT    NOT NULL DEFAULT 'open',
    pnl          REAL    NOT NULL DEFAULT 0,
    mode         TEXT    NOT NULL DEFAULT 'paper',
    mae          REAL    NOT NULL DEFAULT 0,
    -- Trade Journal (broker-execution metadata)
    broker_position_id   TEXT,
    broker_order_id      TEXT,
    execution_latency_ms INTEGER,
    entry_slippage       REAL,
    spread_at_entry      REAL,
    exit_price           REAL,
    exit_slippage        REAL
);

CREATE TABLE IF NOT EXISTS state (
    date           TEXT PRIMARY KEY,
    trades_count   INTEGER NOT NULL DEFAULT 0,
    losses_count   INTEGER NOT NULL DEFAULT 0,
    pnl            REAL    NOT NULL DEFAULT 0,
    drawdown_pct   REAL    NOT NULL DEFAULT 0,
    locked_until_ts TEXT,
    lock_reason    TEXT,
    paused_until_ts TEXT,
    kill_switch    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_signals_ts     ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_ts_open ON trades(ts_open);
CREATE INDEX IF NOT EXISTS idx_trades_symbol  ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_outcome ON trades(outcome);

-- cTrader live feed: raw ticks (bid/ask per symbol)
CREATE TABLE IF NOT EXISTS ticks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT    NOT NULL,          -- ISO-8601 UTC
    symbol   TEXT    NOT NULL,
    bid      REAL    NOT NULL,
    ask      REAL    NOT NULL,
    mid      REAL    NOT NULL,
    spread   REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks(symbol, ts);

-- cTrader live feed: aggregated M1 candles
CREATE TABLE IF NOT EXISTS candles_m1 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    ts_open     TEXT NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    tick_count  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (symbol, ts_open)
);

CREATE INDEX IF NOT EXISTS idx_candles_m1_symbol_ts ON candles_m1(symbol, ts_open);

-- cTrader live feed: aggregated M5 candles
CREATE TABLE IF NOT EXISTS candles_m5 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    ts_open     TEXT NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    tick_count  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (symbol, ts_open)
);

CREATE INDEX IF NOT EXISTS idx_candles_m5_symbol_ts ON candles_m5(symbol, ts_open);

-- Account state (single persistent row, keyed by id=1)
CREATE TABLE IF NOT EXISTS account (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    starting_balance     REAL    NOT NULL DEFAULT 10000.0,
    balance              REAL    NOT NULL DEFAULT 10000.0,
    equity               REAL    NOT NULL DEFAULT 10000.0,
    peak_equity          REAL    NOT NULL DEFAULT 10000.0,
    equity_at_day_start  REAL    NOT NULL DEFAULT 10000.0,
    total_pnl            REAL    NOT NULL DEFAULT 0.0,
    drawdown_pct         REAL    NOT NULL DEFAULT 0.0,
    consecutive_losses   INTEGER NOT NULL DEFAULT 0,
    last_updated         TEXT
);

-- Mode C Expansion state (single persistent row, keyed by id=1)
CREATE TABLE IF NOT EXISTS expansion (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    active              INTEGER NOT NULL DEFAULT 0,
    start_equity        REAL    NOT NULL DEFAULT 0.0,
    trades_in_window    INTEGER NOT NULL DEFAULT 0,
    consecutive_losses  INTEGER NOT NULL DEFAULT 0,
    activated_at        TEXT,
    exit_reason         TEXT,
    atr_spike_active    INTEGER NOT NULL DEFAULT 0
);

-- cTrader OAuth tokens and secrets
CREATE TABLE IF NOT EXISTS secrets (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
