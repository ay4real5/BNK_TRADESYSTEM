"""Application configuration loaded from environment variables."""

from __future__ import annotations

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.enums import Mode, Symbol


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # General
    app_env: str = "dev"
    timezone: str = "Europe/London"
    log_level: str = "INFO"

    # Telegram
    telegram_bot_token: str = "CHANGE_ME"
    telegram_admin_chat_ids: str = ""
    telegram_pin: str = "0000"

    # Symbols & timeframes
    symbols: str = "XAUUSD,XAGUSD"
    entry_tf: str = "15m"
    bias_tf: str = "1h"

    # Account
    account_balance: float = 10000.0          # Starting/reference balance in USD

    # Risk governor
    risk_per_trade_pct: float = 0.5
    max_trades_per_day: int = 2
    max_losses_per_day: int = 2
    daily_dd_cap_pct: float = 2.0
    cooldown_min_after_loss: int = 30
    max_open_positions: int = 2          # total demo/live positions allowed simultaneously

    # Dynamic daily loss limits (equity-based)
    max_daily_loss_pct: float = 2.0           # % of current equity (e.g. 2.0 = $200 on $10k equity)
    max_daily_loss_abs: float | None = None   # Optional hard-floor override in USD (e.g. -250.0)
    max_total_drawdown_pct: float = 10.0      # Kill-switch if equity drops >N% below peak equity

    # Intraday drawdown hard stop (resets at start of each day)
    intraday_dd_stop_pct: float = 5.0         # Hard stop if equity drops >N% from today's open

    # Consecutive-loss risk scaling
    consecutive_loss_threshold: int = 3       # Number of consecutive losses before scaling kicks in
    consecutive_loss_scale_factor: float = 0.5  # Risk per trade multiplied by this after streak

    # -----------------------------------------------------------------------
    # Mode C — Defensive Core + Statistical Expansion Layer
    # -----------------------------------------------------------------------
    # Defensive (default) risk
    defensive_risk_pct: float = 0.5           # % equity per trade in defensive mode

    # Expansion activation gates (all four must be true simultaneously)
    expansion_min_win_rate: float = 0.60      # Rolling win rate >= 60% over last N trades
    expansion_max_dd_pct: float = 3.0         # Max rolling drawdown <= 3% over last N trades
    expansion_rolling_window: int = 30        # Rolling window size (trade count)
    expansion_min_trades: int = 30            # Minimum trades before activation eligible
    expansion_atr_multiplier: float = 1.5     # ATR must be <= N * rolling avg ATR to pass

    # Expansion parameters
    expansion_risk_pct: float = 0.9           # % equity per trade in expansion mode
    expansion_max_trades: int = 20            # Max trades within one expansion window

    # Expansion exit gates (any one triggers exit)
    expansion_exit_win_rate: float = 0.55     # Rolling win rate drops below 55% → exit
    expansion_exit_consec_losses: int = 3     # 3 consecutive losses in expansion → exit
    expansion_exit_dd_pct: float = 3.0        # Drawdown > 3% from expansion start → exit

    @field_validator("max_daily_loss_abs", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    def effective_risk_pct(self, consecutive_losses: int) -> float:
        """Return risk-per-trade % after applying consecutive-loss scaling."""
        if consecutive_losses >= self.consecutive_loss_threshold:
            return self.defensive_risk_pct * self.consecutive_loss_scale_factor
        return self.defensive_risk_pct

    # Spread / volatility filters
    max_spread_xauusd: float = 0.50   # USD
    max_spread_xagusd: float = 0.05   # USD
    atr_spike_multiplier: float = 2.5  # block if ATR > N * avg ATR

    # Strategy parameters
    ema_bias_period: int = 200
    ema_fast_period: int = 20
    ema_slow_period: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    sl_atr_multiplier: float = 1.2
    tp_rr_ratio: float = 1.8

    # Session windows (UTC hours, inclusive)
    london_open_utc: int = 7
    london_close_utc: int = 16
    ny_open_utc: int = 13
    ny_close_utc: int = 21

    # -----------------------------------------------------------------------
    # Session gate
    # -----------------------------------------------------------------------
    # When enabled, entries are only allowed during London and/or NY windows.
    # Disable for backtesting or manual overrides.
    session_gate_enabled: bool = True
    # Priority window (13–16 UTC): both London and NY open simultaneously.
    # When True, non-overlap sessions are down-weighted; overlap is preferred.
    # (Informational only for now — the gate blocks or allows, doesn't scale.)
    session_prefer_overlap: bool = True

    # -----------------------------------------------------------------------
    # Volatility gate
    # -----------------------------------------------------------------------
    volatility_gate_enabled: bool = True
    # Minimum ATR(M5, 14) in native price units for entry to be allowed.
    # Gold moves ~0.5–2.0 USD per M5 bar in a normal session;
    # below 0.30 = dead market, no edge.
    atr_min_xauusd: float = 0.30
    # Silver (XAGUSD) M5 ATR floor in USD.
    atr_min_xagusd: float = 0.03
    # Maximum tolerable spread expressed as a fraction of current ATR.
    # 0.25 = spread must be < 25% of ATR.  Ensures entry edge > transaction cost.
    spread_atr_max_ratio: float = 0.25

    # -----------------------------------------------------------------------
    # News blackout gate
    # -----------------------------------------------------------------------
    # Minutes to block BEFORE and AFTER a high-impact event.
    news_blackout_minutes: int = 15
    # Path to the persisted JSON file of scheduled news events.
    news_events_file: str = "data/news_events.json"

    # -----------------------------------------------------------------------
    # Gold Sniper Model — gold_sniper_pullback_v1
    # -----------------------------------------------------------------------
    # When True, evaluate_strategy ignores non-XAUUSD symbols entirely.
    gold_only_mode: bool = True
    # Analytics tag written into every TradeIdea produced by this model.
    gold_sniper_model_type: str = "gold_sniper_pullback_v1"
    # Minimum R:R enforced inside the rule layer (hard rejection, not just scoring).
    min_rr_to_execute: float = 1.5
    # Enable VWAP as a pullback anchor (in addition to EMA20).
    vwap_enabled: bool = True
    # How many bars of 15m data to use when detecting swing structure.
    structure_lookback_candles: int = 10

    # Trading mode
    mode: Mode = Mode.ASSIST

    # Database
    database_url: str = "sqlite+aiosqlite:///data/trading.db"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Market data source:
    #   "internal"  — SyntheticDataProvider / demo engine (default)
    #   "ctrader"   — live/demo cTrader Open API feed (Phase 4+)
    market_data_source: str = "internal"

    # cTrader OAuth & API Configuration
    ctrader_client_id: str = ""
    ctrader_client_secret: str = ""
    ctrader_redirect_uri: str = "http://localhost:8000/api/v1/auth/ctrader/callback"
    ctrader_env: str = "demo"      # "demo" or "live"
    ctrader_access_token: str = ""
    ctrader_refresh_token: str = ""
    ctrader_token_expires_at: str = ""
    ctrader_account_id: str = ""
    ctrader_demo: bool = True      # True = demo server / False = live server

    # Signal snooze / expiry
    signal_snooze_minutes: int = 30
    signal_expiry_minutes: int = 60

    # Demo engine (set BNK_DEMO_ENGINE=1 to enable)
    bnk_demo_engine: bool = False

    # Test mode (set BNK_TEST_MODE=1 to bypass cooldowns in demo ONLY - for single test trades)
    bnk_test_mode: bool = False

    # -----------------------------------------------------------------------
    # Autonomous execution (AUTO_EXECUTE_DEMO=1 to enable)
    # -----------------------------------------------------------------------
    # When True and MODE=demo, high-scoring signals are auto-sent to cTrader
    # without any manual curl / Telegram command.
    auto_execute_demo: bool = False

    # Minimum strategy score (0–10) required before auto-execution is considered.
    # Signals below this threshold are recorded but never auto-fired.
    min_score_to_execute: float = 8.0

    # How often (seconds) to poll for new signals + try execution in demo mode.
    auto_execute_interval_sec: int = 15

    # How often (seconds) to poll broker for closed positions (SL/TP hits).
    position_sync_interval_sec: int = 30

    @field_validator("mode", mode="before")
    @classmethod
    def lower_mode(cls, v) -> str:
        if hasattr(v, "value"):
            return str(v.value).lower()
        return str(v).lower()

    @property
    def admin_chat_ids(self) -> list[int]:
        if not self.telegram_admin_chat_ids:
            return []
        return [int(x.strip()) for x in self.telegram_admin_chat_ids.split(",") if x.strip()]

    @property
    def active_symbols(self) -> list[Symbol]:
        return [Symbol(s.strip()) for s in self.symbols.split(",") if s.strip()]


# Singleton settings object
settings = Settings()
