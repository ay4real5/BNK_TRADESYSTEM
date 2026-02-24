"""Custom exceptions for the trading system."""


class TradingSystemError(Exception):
    """Base exception for all trading system errors."""


class ConfigError(TradingSystemError):
    """Configuration / environment variable errors."""


class DataFetchError(TradingSystemError):
    """Failed to fetch market data."""


class InsufficientDataError(TradingSystemError):
    """Not enough candle data to compute indicators."""


class StrategyError(TradingSystemError):
    """Error inside strategy evaluation."""


class ExecutionError(TradingSystemError):
    """Error during order execution."""


class RiskViolation(TradingSystemError):
    """A risk-governor rule was violated and trade must be blocked."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LockError(TradingSystemError):
    """Trading is locked and cannot proceed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AuthError(TradingSystemError):
    """Telegram authentication / authorization error."""


class BrokerError(TradingSystemError):
    """Error communicating with the broker (cTrader)."""


class StorageError(TradingSystemError):
    """Database / persistence error."""
