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

    # Risk governor
    risk_per_trade_pct: float = 0.5
    max_trades_per_day: int = 1
    max_losses_per_day: int = 1
    daily_dd_cap_pct: float = 2.0
    cooldown_min_after_loss: int = 180

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

    # Trading mode
    mode: Mode = Mode.ASSIST

    # Database
    database_url: str = "sqlite+aiosqlite:///data/trading.db"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # cTrader (optional)
    ctrader_client_id: str = ""
    ctrader_client_secret: str = ""
    ctrader_access_token: str = ""
    ctrader_account_id: str = ""

    # Signal snooze / expiry
    signal_snooze_minutes: int = 30
    signal_expiry_minutes: int = 60

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
