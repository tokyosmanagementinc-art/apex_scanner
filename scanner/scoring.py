"""
scoring.py  –  Signal Scoring Engine
======================================
Produces a LONG score (0–100) and SHORT score (0–100) for each candidate.

Architecture:
  • Each signal component returns a partial score (0–1) and a direction.
  • Partial scores are multiplied by their weight.
  • Weights are adjusted dynamically based on market regime.
  • Conflicting signals reduce the final score (penalty system).
  • Result is normalised to 0–100.

Signal hierarchy (importance order):
  1. Trend alignment      (primary filter)
  2. Volume confirmation  (secondary filter)
  3. Momentum quality     (strength)
  4. Relative strength    (vs SPY/sector)
  5. Setup quality        (entry timing)
"""

import math
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from scanner.indicators import IndicatorBundle
from scanner.market_regime import MarketRegime
from scanner.support_resistance import SRBundle
from scanner.config import CONFIG
from scanner.utils import logger


# ── Score components ───────────────────────────────────────────────────────────

@dataclass
class ScoreComponent:
    name:       str
    raw_score:  float    # 0.0–1.0
    weight:     float
    direction:  str      # 'long' | 'short' | 'neutral'
    note:       str = ""

    def weighted(self) -> float:
        return self.raw_score * self.weight


@dataclass
class SignalScore:
    symbol:     str
    long_score:     float = 0.0    # 0–100
    short_score:    float = 0.0    # 0–100
    composite:      float = 0.0    # = max(long, short) after penalty
    direction:      str   = "LONG" # 'LONG' | 'SHORT' | 'WATCH'
    confidence:     str   = "LOW"  # 'LOW' | 'MEDIUM' | 'HIGH' | 'VERY HIGH'

    components:     List[ScoreComponent] = field(default_factory=list)
    flags:          List[str]            = field(default_factory=list)
    penalties:      float                = 0.0
    regime_adj:     float                = 1.0

    def add_flag(self, f: str): self.flags.append(f)

    def confidence_label(self) -> str:
        if self.composite >= 80:  return "VERY HIGH"
        if self.composite >= 68:  return "HIGH"
        if self.composite >= 55:  return "MEDIUM"
        return "LOW"


# ── Component scorers ──────────────────────────────────────────────────────────

def _weight(name: str, market_session: str) -> float:
    if market_session in ("pre-market", "after-hours"):
        return getattr(CONFIG, f"{name}_EXTENDED", getattr(CONFIG, name))
    return getattr(CONFIG, name)


def _score_trend(ind: IndicatorBundle, regime: MarketRegime, market_session: str) -> ScoreComponent:
    """EMA stack alignment + trend direction."""
    score = 0.0
    direction = "neutral"

    if ind.ema_stack_bull:
        score += 0.6
        direction = "long"
    elif ind.trend_dir == "bullish":
        score += 0.35
        direction = "long"
    elif ind.trend_dir == "bearish":
        score += 0.5   # good for shorts
        direction = "short"

    # ADX confirms trend strength (>25 = trending)
    if not np.isnan(ind.adx_val):
        if ind.adx_val > 30:
            score += 0.3
        elif ind.adx_val > 20:
            score += 0.15

    # +DI vs -DI direction confirmation
    if not np.isnan(ind.plus_di) and not np.isnan(ind.minus_di):
        if ind.plus_di > ind.minus_di and direction == "long":
            score += 0.1
        elif ind.minus_di > ind.plus_di and direction == "short":
            score += 0.1

    return ScoreComponent("trend", min(score, 1.0), _weight("W_TECHNICAL", market_session) * 0.4, direction)


def _score_momentum(ind: IndicatorBundle, price_change: float, market_session: str) -> ScoreComponent:
    """RSI + MACD + price momentum."""
    score = 0.0
    direction = "neutral"

    rsi = ind.rsi_val if not np.isnan(ind.rsi_val) else 50.0
    macd_h = ind.macd_hist if not np.isnan(ind.macd_hist) else 0.0

    # RSI momentum zones (not overbought/oversold extremes)
    if 50 <= rsi <= 70:
        score += 0.4
        direction = "long"
    elif 30 <= rsi < 50:
        score += 0.2
        direction = "short"
    elif rsi > 70:
        # overbought – still can work in strong momentum
        score += 0.25
        direction = "long"
    elif rsi < 30:
        # oversold – reversal potential
        score += 0.3
        direction = "long"

    # RSI slope (rising RSI = positive momentum)
    if ind.rsi_slope > 0.5:
        score += 0.2
    elif ind.rsi_slope < -0.5:
        score -= 0.1

    # MACD histogram above zero
    if macd_h > 0:
        score += 0.25
    elif ind.macd_cross_up:
        score += 0.35
        direction = "long"

    # Price change today
    if price_change > 3:
        score += 0.15
    elif price_change < -3:
        score += 0.1
        direction = "short"

    return ScoreComponent("momentum", min(max(score, 0.0), 1.0),
                          _weight("W_MOMENTUM", market_session), direction)


def _score_volume(ind: IndicatorBundle, market_session: str) -> ScoreComponent:
    """Relative volume and VWAP relationship."""
    score = 0.0
    direction = "neutral"

    rvol = ind.rel_vol if not np.isnan(ind.rel_vol) else 1.0

    # Relative volume bonus
    if rvol >= 4.0:
        score += 0.80
    elif rvol >= 3.0:
        score += 0.65
    elif rvol >= 2.0:
        score += 0.45
    elif rvol >= 1.5:
        score += 0.25
    elif rvol < 0.8:
        score = 0.05   # low volume = bad signal

    # VWAP: price above VWAP = bullish control
    pv = ind.price_vs_vwap
    if not np.isnan(pv):
        if pv > 0.5:
            score += 0.2
            direction = "long"
        elif pv < -0.5:
            direction = "short"

    return ScoreComponent("volume", min(score, 1.0), _weight("W_VOLUME", market_session), direction)


def _score_setup(ind: IndicatorBundle, sr: Optional[SRBundle], market_session: str) -> ScoreComponent:
    """Breakout, squeeze, BB position, entry quality."""
    score = 0.0
    direction = "neutral"
    note = ""

    # Breakout above resistance
    if ind.breakout_signal == "up":
        score += 0.60
        direction = "long"
        note = "breakout_up"
    elif ind.breakout_signal == "down":
        score += 0.50
        direction = "short"
        note = "breakout_down"

    # Squeeze firing (Bollinger squeeze releasing)
    if ind.squeeze_firing:
        score += 0.30
        note += "+squeeze_fire"

    # BB position (0=oversold, 1=overbought)
    bb = ind.bb_pct_b_val if not np.isnan(ind.bb_pct_b_val) else 0.5
    if 0.45 <= bb <= 0.80:
        score += 0.15   # healthy range
    elif bb > 0.95:
        score -= 0.10   # stretched
    elif bb < 0.05:
        score += 0.20   # potential mean reversion long

    # S/R proximity bonus: near support = good long entry
    if sr:
        if sr.near_support(threshold_pct=1.5):
            score += 0.20
            direction = "long"
            note += "+near_support"
        if sr.near_resistance(threshold_pct=1.0):
            score -= 0.10   # crowded near resistance
            note += "-near_resistance"

    # StochRSI cross up
    if not np.isnan(ind.stoch_k) and not np.isnan(ind.stoch_d):
        if ind.stoch_k > ind.stoch_d and ind.stoch_k < 80:
            score += 0.10
        elif ind.stoch_k < 20:
            score += 0.15   # oversold reversal zone

    return ScoreComponent("setup", min(max(score, 0.0), 1.0),
                          _weight("W_TECHNICAL", market_session) * 0.6, direction, note)


def _score_relative_strength(symbol: str, change_pct: float,
                              spy_change: float = 0.0,
                              sector_change: float = 0.0,
                              market_session: str = "regular") -> ScoreComponent:
    """RS vs SPY and sector ETF."""
    rs_vs_spy    = change_pct - spy_change
    rs_vs_sector = change_pct - sector_change
    score = 0.0
    direction = "neutral"

    # Outperforming SPY
    if rs_vs_spy > 2.0:
        score += 0.50
        direction = "long"
    elif rs_vs_spy > 0.5:
        score += 0.25
        direction = "long"
    elif rs_vs_spy < -2.0:
        score += 0.40
        direction = "short"

    # Outperforming sector
    if rs_vs_sector > 1.0:
        score += 0.30
    elif rs_vs_sector < -1.0:
        score -= 0.10

    return ScoreComponent("relative_strength", min(max(score, 0.0), 1.0),
                          _weight("W_RS", market_session), direction)


def _score_sector_alignment(sector: str, regime: MarketRegime, market_session: str) -> ScoreComponent:
    """Is the stock's sector the strongest/weakest today?"""
    score = 0.5   # neutral default
    direction = "neutral"

    if not regime.sector_scores or not sector:
        return ScoreComponent("sector", score, _weight("W_SECTOR", market_session), direction)

    # Map sector name → closest ETF
    _map = {
        "Technology":           "XLK",
        "Financial Services":   "XLF",
        "Energy":               "XLE",
        "Healthcare":           "XLV",
        "Industrials":          "XLI",
        "Consumer Defensive":   "XLP",
        "Utilities":            "XLU",
        "Basic Materials":      "XLB",
        "Communication Services":"XLC",
        "Real Estate":          "XLRE",
        "Consumer Cyclical":    "XLC",
        "Semiconductor":        "SMH",
        "Biotechnology":        "XBI",
    }
    etf = _map.get(sector)
    if not etf or etf not in regime.sector_scores:
        return ScoreComponent("sector", score, _weight("W_SECTOR", market_session), direction)

    sector_return = regime.sector_scores[etf]
    median_return = float(np.median(list(regime.sector_scores.values())))

    if sector_return > median_return + 1.0:
        score = 0.9
        direction = "long"
    elif sector_return > median_return:
        score = 0.65
        direction = "long"
    elif sector_return < median_return - 1.0:
        score = 0.2
        direction = "short"
    else:
        score = 0.4

    return ScoreComponent("sector", score, CONFIG.W_SECTOR, direction)


def _score_squeeze_play(ind: IndicatorBundle) -> Optional[ScoreComponent]:
    """Short squeeze overlay – adds bonus score if setup is present."""
    if ind.squeeze_score < 30:
        return None
    score = ind.squeeze_score / 100
    return ScoreComponent("squeeze_play", score, 0.10, "long",
                          f"squeeze_prob={ind.squeeze_score:.0f}")


# ── Penalty engine ────────────────────────────────────────────────────────────

def _apply_penalties(components: List[ScoreComponent]) -> float:
    """
    Detect conflicting signals and apply a penalty (0–0.25).
    E.g. volume says short, trend says long → conflict.
    """
    dirs = [c.direction for c in components if c.direction != "neutral"]
    if not dirs:
        return 0.0
    long_votes  = dirs.count("long")
    short_votes = dirs.count("short")
    total = long_votes + short_votes
    if total == 0:
        return 0.0
    conflict_ratio = min(long_votes, short_votes) / total
    return conflict_ratio * 0.25    # max 25% penalty


# ── Main scorer ───────────────────────────────────────────────────────────────

def compute_score(
    symbol:        str,
    ind:           IndicatorBundle,
    sr:            Optional[SRBundle],
    regime:        MarketRegime,
    price_change:  float = 0.0,
    sector:        str   = "",
    spy_change:    float = 0.0,
    sector_change: float = 0.0,
    market_session: str = "regular",
) -> SignalScore:
    """
    Compute final LONG/SHORT scores for one symbol.
    Returns a SignalScore object with full component breakdown.
    """
    sig = SignalScore(symbol=symbol)

    # ── Build all components ──
    components = [
        _score_trend(ind, regime, market_session),
        _score_momentum(ind, price_change, market_session),
        _score_volume(ind, market_session),
        _score_setup(ind, sr, market_session),
        _score_relative_strength(symbol, price_change, spy_change, sector_change, market_session),
        _score_sector_alignment(sector, regime, market_session),
    ]

    # Optional squeeze overlay
    sq = _score_squeeze_play(ind)
    if sq:
        components.append(sq)
        sig.add_flag("SHORT_SQUEEZE_CANDIDATE")

    sig.components = components

    # ── Weighted raw scores ──
    long_raw  = 0.0
    short_raw = 0.0
    for c in components:
        if c.direction in ("long", "neutral"):
            long_raw  += c.weighted()
        if c.direction in ("short", "neutral"):
            short_raw += c.weighted()

    total_weight = sum(c.weight for c in components)
    long_raw     = long_raw  / total_weight if total_weight else 0
    short_raw    = short_raw / total_weight if total_weight else 0

    # ── Regime adjustment ──
    sig.regime_adj = regime.momentum_weight_adj
    long_raw  *= regime.momentum_weight_adj
    short_raw *= regime.reversal_weight_adj

    # ── Penalties ──
    sig.penalties = _apply_penalties(components)
    long_raw  *= (1 - sig.penalties)
    short_raw *= (1 - sig.penalties)

    # ── Normalise to 0–100 ──
    sig.long_score  = round(min(long_raw  * 100, 100), 1)
    sig.short_score = round(min(short_raw * 100, 100), 1)

    # ── Direction & composite ──
    if sig.long_score >= sig.short_score:
        sig.direction = "LONG"
        sig.composite = sig.long_score
    else:
        sig.direction = "SHORT"
        sig.composite = sig.short_score

    # ── Flag generation ──
    if ind.breakout_signal == "up":
        sig.add_flag("BREAKOUT")
    if ind.squeeze_firing:
        sig.add_flag("SQUEEZE_FIRE")
    if not np.isnan(ind.rel_vol) and ind.rel_vol >= 3:
        sig.add_flag(f"VOL_SURGE_{ind.rel_vol:.1f}x")
    if not np.isnan(ind.rsi_val) and ind.rsi_val < 35:
        sig.add_flag("OVERSOLD")
    if not np.isnan(ind.rsi_val) and ind.rsi_val > 75:
        sig.add_flag("OVERBOUGHT")

    sig.confidence = sig.confidence_label()
    return sig


# ── Batch ranker ──────────────────────────────────────────────────────────────

def rank_signals(scores: List[SignalScore],
                 min_score: float = 50.0,
                 direction_filter: Optional[str] = None) -> List[SignalScore]:
    """
    Filter and rank SignalScore list.
    direction_filter: 'LONG' | 'SHORT' | None (both)
    """
    result = [s for s in scores if s.composite >= min_score]
    if direction_filter:
        result = [s for s in result if s.direction == direction_filter]
    result.sort(key=lambda s: s.composite, reverse=True)
    return result
