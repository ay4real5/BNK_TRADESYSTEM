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
    mae          REAL    NOT NULL DEFAULT 0
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
