"""
Analyzer service.

Orchestrates the full cycle:
  fetch candles → compute features → evaluate strategy →
  apply risk/lock checks → create TradeIdea → dispatch Telegram alert
"""

from __future__ import annotations

from loguru import logger

from ..config import settings
from ..data import market_data, storage
from ..domain.enums import Mode, Symbol
from ..domain.errors import LockError, InsufficientDataError, DataFetchError
from ..domain.models import TradeIdea
from ..execution.safeguards import run_all_safeguards
from ..services import locks, news_filter
from ..strategy.rules import evaluate_strategy


# Telegram bot reference — injected at startup to avoid circular imports
_telegram_notify = None


def set_telegram_notify(callback) -> None:
    """Inject the async Telegram notification callback."""
    global _telegram_notify
    _telegram_notify = callback


async def run_analysis_cycle(db_path: str = storage.DB_PATH) -> list[TradeIdea]:
    """
    Execute one full analysis cycle across all configured symbols.
    Returns a list of TradeIdeas generated (may be empty).
    """
    found: list[TradeIdea] = []

    for symbol in settings.active_symbols:
        idea = await _analyze_symbol(symbol, db_path=db_path)
        if idea is not None:
            found.append(idea)

    return found


async def _analyze_symbol(
    symbol: Symbol,
    db_path: str = storage.DB_PATH,
) -> TradeIdea | None:
    """Run full analysis for a single symbol. Returns TradeIdea or None."""
    try:
        # 1. Fetch candles
        candles_15m = await market_data.fetch_candles(symbol, settings.entry_tf, count=300)
        candles_1h = await market_data.fetch_candles(symbol, settings.bias_tf, count=300)
        spread = await market_data.fetch_spread(symbol)
    except (DataFetchError, Exception) as exc:
        logger.error("Data fetch failed for {}: {}", symbol.value, exc)
        return None

    # 2. Check locks BEFORE doing any heavy work
    try:
        state = await locks.check_can_trade()
    except LockError as lock_err:
        logger.info("{}: trading locked — {}", symbol.value, lock_err.reason)
        return None

    # 3. Check news filter
    news_blocked, news_reason = await news_filter.is_news_window()
    if news_blocked:
        logger.info("{}: {}", symbol.value, news_reason)
        return None

    # 4. Check spread
    max_spread = (
        settings.max_spread_xauusd
        if symbol == Symbol.XAUUSD
        else settings.max_spread_xagusd
    )
    if spread > max_spread:
        logger.info("{}: spread {:.4f} too wide (max {:.4f})", symbol.value, spread, max_spread)
        return None

    # 5. Evaluate strategy
    try:
        idea = evaluate_strategy(symbol, candles_15m, candles_1h, settings.mode)
    except InsufficientDataError as exc:
        logger.debug("{}: {}", symbol.value, exc)
        return None
    except Exception as exc:
        logger.error("Strategy error for {}: {}", symbol.value, exc)
        return None

    if idea is None:
        return None

    idea.score = idea.score  # already set by scorer

    # 6. Run safeguards
    volatility = (
        candles_15m[-1].close  # placeholder — regime is in context
    )
    from ..strategy.regime import classify_volatility
    from ..data.market_data import candles_to_df
    df_15m = candles_to_df(candles_15m)
    vol_regime = classify_volatility(df_15m)

    try:
        run_all_safeguards(idea, spread, vol_regime)
    except Exception as safeguard_exc:
        logger.info("{}: safeguard blocked trade — {}", symbol.value, safeguard_exc)
        return None

    # 7. Persist signal
    try:
        idea.id = await storage.save_signal(idea, db_path=db_path)
    except Exception as exc:
        logger.error("Failed to persist signal: {}", exc)

    # 8. Notify via Telegram
    if _telegram_notify is not None:
        try:
            await _telegram_notify(idea)
        except Exception as exc:
            logger.error("Telegram notify failed: {}", exc)

    return idea
