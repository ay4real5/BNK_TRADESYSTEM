"""
Setup scoring engine.

Takes a MarketContext + candidate TradeIdea and scores the quality
of the setup from 0 to 10, with reasons.
"""

from __future__ import annotations

from ..domain.enums import Bias, Side
from ..domain.models import MarketContext, TradeIdea


def score_setup(ctx: MarketContext, idea: TradeIdea) -> tuple[float, list[str]]:
    """
    Score a trading setup from 0-10.

    Returns (score, reasons_list).
    Each criterion adds points; the final score is capped at 10.
    """
    score = 0.0
    reasons: list[str] = []

    # 1. Bias alignment (2 pts)
    if (ctx.bias == Bias.BULLISH and idea.side == Side.BUY) or \
       (ctx.bias == Bias.BEARISH and idea.side == Side.SELL):
        score += 2.0
        reasons.append("✅ Trade aligns with 1H bias")
    else:
        reasons.append("⚠️ Trade against 1H bias — caution")

    # 2. RSI confirmation (1.5 pts)
    if idea.side == Side.BUY and ctx.rsi_15m > 50:
        score += 1.5
        reasons.append(f"✅ RSI {ctx.rsi_15m:.1f} > 50 (bullish momentum)")
    elif idea.side == Side.SELL and ctx.rsi_15m < 50:
        score += 1.5
        reasons.append(f"✅ RSI {ctx.rsi_15m:.1f} < 50 (bearish momentum)")
    else:
        reasons.append(f"⚠️ RSI {ctx.rsi_15m:.1f} not confirming direction")

    # 3. Risk-reward quality (2 pts)
    if idea.risk_reward >= 1.8:
        score += 2.0
        reasons.append(f"✅ RR {idea.risk_reward:.2f}:1 (≥1.8)")
    elif idea.risk_reward >= 1.5:
        score += 1.0
        reasons.append(f"ℹ️ RR {idea.risk_reward:.2f}:1 (acceptable)")
    else:
        reasons.append(f"❌ RR {idea.risk_reward:.2f}:1 too low")

    # 4. Session (1 pt)
    if ctx.is_london_session or ctx.is_ny_session:
        score += 1.0
        session = "London" if ctx.is_london_session else "NY"
        reasons.append(f"✅ Active session ({session})")
    else:
        reasons.append("⚠️ Outside London/NY session")

    # 5. Volatility regime (1.5 pts)
    if ctx.volatility_regime == "normal":
        score += 1.5
        reasons.append("✅ Normal volatility")
    elif ctx.volatility_regime == "low":
        score += 0.5
        reasons.append("ℹ️ Low volatility — smaller moves expected")
    elif ctx.volatility_regime == "high":
        score += 0.5
        reasons.append("⚠️ High volatility — use tight risk")
    else:
        reasons.append("❌ Extreme volatility — trade blocked by filter")

    # 6. EMA proximity (1 pt)
    if idea.side == Side.BUY and ctx.price <= ctx.ema20_15m * 1.002:
        score += 1.0
        reasons.append("✅ Price near EMA20 (pullback entry)")
    elif idea.side == Side.SELL and ctx.price >= ctx.ema20_15m * 0.998:
        score += 1.0
        reasons.append("✅ Price near EMA20 (pullback entry)")

    # 7. Spread quality (1 pt)
    from ..config import settings
    max_spread = (
        settings.max_spread_xauusd
        if idea.symbol.value == "XAUUSD"
        else settings.max_spread_xagusd
    )
    if ctx.spread <= max_spread * 0.5:
        score += 1.0
        reasons.append(f"✅ Tight spread ({ctx.spread:.4f})")
    elif ctx.spread <= max_spread:
        score += 0.5
        reasons.append(f"ℹ️ Acceptable spread ({ctx.spread:.4f})")
    else:
        reasons.append(f"❌ Wide spread ({ctx.spread:.4f})")

    score = min(score, 10.0)
    return round(score, 1), reasons
