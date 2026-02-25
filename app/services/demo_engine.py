"""
Background demo signal engine.

Generates synthetic trading signals every 15 seconds and persists them via
the storage layer.  Enabled only when the environment variable
``BNK_DEMO_ENGINE=1`` is set.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone

from loguru import logger

from ..data import storage
from ..domain.enums import Bias, Mode, Side, SignalStatus, Symbol
from ..domain.models import TradeIdea
from ..integration.candle_builder import CandleBuilder, Tick

# Initialize candle builders (one per symbol-timeframe pair)
_candle_builders: dict[tuple[Symbol, int], CandleBuilder] = {}

def _get_candle_builder(symbol: Symbol, tf_min: int) -> CandleBuilder:
    """Get or create candle builder for symbol and timeframe."""
    key = (symbol, tf_min)
    if key not in _candle_builders:
        _candle_builders[key] = CandleBuilder(symbol, tf_min)
    return _candle_builders[key]

# ---------------------------------------------------------------------------
# Synthetic price parameters
# ---------------------------------------------------------------------------

_SYMBOL_PARAMS: dict[Symbol, dict] = {
    Symbol.XAUUSD: {
        "base": 2350.0,
        "spread": 40.0,   # price wanders ± this amount
        "spread_pips": 0.50,  # bid-ask spread
        "sl_pts": 8.0,
        "tp_mult": 1.8,
    },
    Symbol.XAGUSD: {
        "base": 28.50,
        "spread": 0.60,
        "spread_pips": 0.03,  # bid-ask spread
        "sl_pts": 0.15,
        "tp_mult": 1.8,
    },
}

# Current synthetic prices (maintained across ticks)
_CURRENT_PRICES: dict[Symbol, float] = {
    Symbol.XAUUSD: _SYMBOL_PARAMS[Symbol.XAUUSD]["base"],
    Symbol.XAGUSD: _SYMBOL_PARAMS[Symbol.XAGUSD]["base"],
}

_DEMO_REASONS = [
    "EMA200 bias confirmed",
    "EMA20/50 pullback entry",
    "RSI cross signal",
    "London session breakout",
    "NY open momentum",
    "Structure support hold",
    "Bearish engulfing rejection",
    "Bullish pin bar close",
    "ATR volatility within range",
    "Higher-high continuation",
]

INTERVAL_SECONDS: int = 15
TICK_INTERVAL_SECONDS: float = 2.0  # Generate ticks every 2 seconds


# ---------------------------------------------------------------------------
# Tick generation
# ---------------------------------------------------------------------------

async def _generate_synthetic_tick(symbol: Symbol) -> None:
    """Generate and save a single synthetic tick for the given symbol."""
    params = _SYMBOL_PARAMS[symbol]
    
    # Random walk: small movement from current price
    price_change = random.uniform(-0.5, 0.5)  # Small percentage
    _CURRENT_PRICES[symbol] = round(
        _CURRENT_PRICES[symbol] + price_change, 5
    )
    
    mid_price = _CURRENT_PRICES[symbol]
    half_spread = params["spread_pips"] / 2.0
    
    bid = round(mid_price - half_spread, 5)
    ask = round(mid_price + half_spread, 5)
    
    ts = datetime.now(tz=timezone.utc)
    
    try:
        # Save tick to database
        await storage.save_tick(symbol.value, ts, bid, ask)
        
        # Build candles from this tick for m1 and m5
        tick_obj = Tick(ts=ts, bid=bid, ask=ask)
        
        for tf_min in [1, 5]:
            builder = _get_candle_builder(symbol, tf_min)
            completed_candle = builder.on_tick(tick_obj)
            
            # Save completed candle if any
            if completed_candle:
                table = f"candles_m{tf_min}"
                await storage.save_candle(
                    table,
                    symbol.value,
                    completed_candle.ts,  # Use 'ts' not 'ts_open'
                    completed_candle.open,
                    completed_candle.high,
                    completed_candle.low,
                    completed_candle.close,
                    int(completed_candle.volume),  # tick_count
                )
                logger.debug(
                    "DemoEngine completed {} candle: {} @ {}",
                    table,
                    symbol.value,
                    completed_candle.ts
                )
        
    except Exception as exc:
        logger.warning(f"Failed to process tick for {symbol.value}: {exc}")


# ---------------------------------------------------------------------------
# Signal factory
# ---------------------------------------------------------------------------

def _make_demo_signal() -> TradeIdea:
    """Return a single randomly generated synthetic TradeIdea."""
    symbol: Symbol = random.choice(list(Symbol))
    params = _SYMBOL_PARAMS[symbol]

    side: Side = random.choice(list(Side))
    bias: Bias = Bias.BULLISH if side == Side.BUY else Bias.BEARISH

    # Jitter the entry around the base price
    entry = round(params["base"] + random.uniform(-params["spread"], params["spread"]), 5)

    sl_pts = params["sl_pts"] * random.uniform(0.8, 1.4)
    tp_pts = sl_pts * params["tp_mult"]

    if side == Side.BUY:
        sl = round(entry - sl_pts, 5)
        tp = round(entry + tp_pts, 5)
    else:
        sl = round(entry + sl_pts, 5)
        tp = round(entry - tp_pts, 5)

    score = round(random.uniform(5.0, 9.5), 1)
    reasons = random.sample(_DEMO_REASONS, k=random.randint(2, 4))

    return TradeIdea(
        ts=datetime.utcnow(),
        symbol=symbol,
        side=side,
        entry=entry,
        sl=sl,
        tp=tp,
        score=score,
        reasons=reasons,
        mode=Mode.PAPER,
        status=SignalStatus.PENDING,
        bias=bias,
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class DemoEngine:
    """Async background engine that generates synthetic ticks and signals."""

    def __init__(self, interval: int = INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._signal_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None

    async def _run_signals(self) -> None:
        """Generate synthetic trading signals periodically."""
        logger.info("DemoEngine started — generating signals every {}s", self._interval)
        while True:
            try:
                signal = _make_demo_signal()
                row_id = await storage.save_signal(signal)
                logger.debug(
                    "DemoEngine saved signal id={} symbol={} side={} score={}",
                    row_id,
                    signal.symbol.value,
                    signal.side.value,
                    signal.score,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("DemoEngine signal error (will retry): {}", exc)

            await asyncio.sleep(self._interval)

    async def _run_ticks(self) -> None:
        """Generate synthetic market ticks periodically."""
        logger.info("DemoEngine tick generator started — generating ticks every {:.1f}s", TICK_INTERVAL_SECONDS)
        while True:
            try:
                # Generate ticks for all symbols
                for symbol in Symbol:
                    await _generate_synthetic_tick(symbol)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("DemoEngine tick error (will retry): {}", exc)

            await asyncio.sleep(TICK_INTERVAL_SECONDS)

    def start(self) -> None:
        """Schedule both signal and tick generation as background asyncio Tasks."""
        if self._signal_task is not None or self._tick_task is not None:
            logger.warning("DemoEngine.start() called but engine is already running")
            return
        self._signal_task = asyncio.create_task(self._run_signals(), name="demo_engine_signals")
        self._tick_task = asyncio.create_task(self._run_ticks(), name="demo_engine_ticks")

    async def stop(self) -> None:
        """Cancel both background tasks and wait for them to finish cleanly."""
        tasks_to_cancel = []
        if self._signal_task is not None:
            tasks_to_cancel.append(self._signal_task)
            self._signal_task = None
        if self._tick_task is not None:
            tasks_to_cancel.append(self._tick_task)
            self._tick_task = None
        
        for task in tasks_to_cancel:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        if tasks_to_cancel:
            logger.info("DemoEngine stopped")


# Module-level singleton — imported by the FastAPI lifespan
demo_engine = DemoEngine()
