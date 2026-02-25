from enum import Enum


class Mode(str, Enum):
    """Trading mode."""
    ASSIST = "assist"
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


class Side(str, Enum):
    """Trade direction."""
    BUY = "buy"
    SELL = "sell"


class Symbol(str, Enum):
    """Tradeable symbols."""
    XAUUSD = "XAUUSD"
    XAGUSD = "XAGUSD"


class SignalStatus(str, Enum):
    """Lifecycle status of a signal."""
    PENDING = "pending"       # Awaiting user confirmation
    CONFIRMED = "confirmed"   # User confirmed
    REJECTED = "rejected"     # User rejected
    SNOOZED = "snoozed"      # User snoozed
    EXPIRED = "expired"       # No action taken, timed out
    EXECUTED = "executed"     # Trade placed (paper/live)


class TradeOutcome(str, Enum):
    """Outcome of a completed trade."""
    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"
    OPEN = "open"
    VOID = "void"          # phantom / pre-fix / orphaned trade (never real)
    CLOSED = "closed"      # closed but win/loss not yet classified (legacy)


class Timeframe(str, Enum):
    """Chart timeframes."""
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


class Bias(str, Enum):
    """Market directional bias."""
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class LockReason(str, Enum):
    """Reason trading is locked."""
    MAX_TRADES = "max_trades_per_day"
    MAX_LOSSES = "max_losses_per_day"
    DAILY_DD = "daily_drawdown_cap"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    TOTAL_DRAWDOWN = "total_drawdown_exceeded"
    INTRADAY_DD_STOP = "intraday_drawdown_stop"
    COOLDOWN = "cooldown_after_loss"
    PAUSED = "manually_paused"
    KILL_SWITCH = "kill_switch"
    HIGH_VOLATILITY = "high_volatility"
    NEWS_FILTER = "news_filter"
