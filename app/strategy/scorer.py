"""
Setup scoring engine — Gold Sniper Pullback v1.

Scores a setup from 0–10.  The execution threshold is 8.0 (min_score_to_execute).
A clean, well-aligned setup will reach 8.5–10; marginal setups stay below 7.

Scoring criteria (max 10 points):
  1. 1H bias alignment          — 2.0 pts
  2. 15m structure alignment    — 1.5 pts  ← NEW: HH+HL or LH+LL must agree
  3. VWAP pullback              — 1.0 pt   ← NEW: price at/near VWAP = value entry
  4. RSI confirmation           — 1.0 pt
  5. R:R quality                — 2.0 pts
  6. Session quality            — 0.5 pt
  7. Volatility regime          — 1.0 pt
  8. Spread quality             — 1.0 pt
  ─────────────────────────────────────────
  Total max                     = 10.0 pts
"""

from __future__ import annotations

from ..domain.enums import Bias, Side
from ..domain.models import MarketContext, TradeIdea


def score_setup(ctx: MarketContext, idea: TradeIdea) -> tuple[float, list[str]]:
    """
    Score a trading setup from 0–10.

    Returns (score, reasons_list).
    """
    score = 0.0
    reasons: list[str] = []

    # ── 0. Model tag (informational, no points) ──────────────────────────
    if idea.model_type:
        reasons.append(f"📋 model={idea.model_type}")
    if idea.session_label:
        session_map = {
            "london": "London (07–16 UTC)",
            "ny": "NY (13–21 UTC)",
            "overlap": "London/NY overlap (13–16 UTC)",
        }
        reasons.append(f"🕐 session={session_map.get(idea.session_label, idea.session_label)}")

    # ── 1. 1H bias alignment (2 pts) ─────────────────────────────────────
    if (ctx.bias == Bias.BULLISH and idea.side == Side.BUY) or \
       (ctx.bias == Bias.BEARISH and idea.side == Side.SELL):
        score += 2.0
        reasons.append("✅ Trade aligns with 1H EMA200 bias")
    else:
        reasons.append("⚠️ Trade against 1H bias — caution")

    # ── 2. 15m swing structure alignment (1.5 pts) ───────────────────────
    expected = "bullish" if idea.side == Side.BUY else "bearish"
    if ctx.structure_15m == expected:
        score += 1.5
        reasons.append(f"✅ 15m structure {ctx.structure_15m} (HH+HL / LH+LL confirmed)")
    elif ctx.structure_15m == "neutral":
        score += 0.5
        reasons.append("ℹ️ 15m structure neutral — weak confirmation")
    else:
        reasons.append(f"❌ 15m structure {ctx.structure_15m} disagrees with trade direction")

    # ── 3. VWAP pullback (1 pt) ───────────────────────────────────────────
    if ctx.vwap > 0:
        vwap_dist_pct = abs(ctx.price - ctx.vwap) / ctx.vwap
        if idea.side == Side.BUY and ctx.price <= ctx.vwap * 1.001:
            score += 1.0
            reasons.append(f"✅ Price at/below VWAP {ctx.vwap:.2f} — value area buy")
        elif idea.side == Side.SELL and ctx.price >= ctx.vwap * 0.999:
            score += 1.0
            reasons.append(f"✅ Price at/above VWAP {ctx.vwap:.2f} — value area sell")
        elif vwap_dist_pct <= 0.003:
            score += 0.5
            reasons.append(f"ℹ️ Price near VWAP {ctx.vwap:.2f} ({vwap_dist_pct*100:.2f}% away)")
        else:
            reasons.append(f"⚠️ Price {ctx.price:.2f} far from VWAP {ctx.vwap:.2f} ({vwap_dist_pct*100:.2f}%)")
    else:
        # VWAP unavailable — check EMA20 proximity instead
        if ctx.ema20_15m > 0:
            ema_dist = abs(ctx.price - ctx.ema20_15m) / ctx.ema20_15m
            if ema_dist <= 0.003:
                score += 0.5
                reasons.append(f"ℹ️ Pullback to EMA20 {ctx.ema20_15m:.2f} (no VWAP)")
            else:
                reasons.append("⚠️ No VWAP and price far from EMA20")

    # ── 4. RSI confirmation (1 pt) ────────────────────────────────────────
    if idea.side == Side.BUY and ctx.rsi_15m > 50:
        score += 1.0
        reasons.append(f"✅ RSI {ctx.rsi_15m:.1f} > 50 (bullish momentum)")
    elif idea.side == Side.SELL and ctx.rsi_15m < 50:
        score += 1.0
        reasons.append(f"✅ RSI {ctx.rsi_15m:.1f} < 50 (bearish momentum)")
    else:
        reasons.append(f"⚠️ RSI {ctx.rsi_15m:.1f} not confirming direction")

    # ── 5. R:R quality (2 pts) ────────────────────────────────────────────
    if idea.risk_reward >= 2.0:
        score += 2.0
        reasons.append(f"✅ RR {idea.risk_reward:.2f}:1 (≥2.0 — premium setup)")
    elif idea.risk_reward >= 1.8:
        score += 1.5
        reasons.append(f"✅ RR {idea.risk_reward:.2f}:1 (≥1.8)")
    elif idea.risk_reward >= 1.5:
        score += 1.0
        reasons.append(f"ℹ️ RR {idea.risk_reward:.2f}:1 (minimum acceptable)")
    else:
        reasons.append(f"❌ RR {idea.risk_reward:.2f}:1 too low (< 1.5)")

    # ── 6. Session quality (0.5 pt) ───────────────────────────────────────
    if ctx.is_london_session and ctx.is_ny_session:
        score += 0.5
        reasons.append("✅ London/NY overlap — peak liquidity")
    elif ctx.is_london_session:
        score += 0.4
        reasons.append("✅ London session")
    elif ctx.is_ny_session:
        score += 0.4
        reasons.append("✅ NY session")
    else:
        reasons.append("⚠️ Outside London/NY — lower liquidity")

    # ── 7. Volatility regime (1 pt) ───────────────────────────────────────
    if ctx.volatility_regime == "normal":
        score += 1.0
        reasons.append("✅ Normal volatility — clean conditions")
    elif ctx.volatility_regime == "high":
        score += 0.5
        reasons.append("⚠️ High volatility — use tight risk")
    elif ctx.volatility_regime == "low":
        score += 0.25
        reasons.append("ℹ️ Low volatility — smaller moves expected")
    else:
        reasons.append("❌ Extreme volatility")

    # ── 8. Spread quality (1 pt) ──────────────────────────────────────────
    from ..config import settings
    max_spread = (
        settings.max_spread_xauusd
        if idea.symbol.value == "XAUUSD"
        else settings.max_spread_xagusd
    )
    if ctx.spread == 0:
        # Spread unknown (synthetic/demo data) — neutral
        score += 0.5
        reasons.append("ℹ️ Spread unknown (demo mode)")
    elif ctx.spread <= max_spread * 0.5:
        score += 1.0
        reasons.append(f"✅ Tight spread ({ctx.spread:.4f})")
    elif ctx.spread <= max_spread:
        score += 0.5
        reasons.append(f"ℹ️ Acceptable spread ({ctx.spread:.4f})")
    else:
        reasons.append(f"❌ Wide spread ({ctx.spread:.4f})")

    score = min(score, 10.0)
    return round(score, 1), reasons
