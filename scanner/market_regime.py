"""
market_regime.py  –  Market Regime Detection
=============================================
Classifies the current market environment using free yfinance data:
  SPY trend  ×  VIX level  ×  breadth proxy  →  Regime label

Regimes:
  BULL       – strong uptrend, low volatility → favor momentum/breakout
  BULL_CHOP  – uptrend but elevated vol       → require stronger signals
  BEAR       – downtrend                      → tighten entries, favor shorts
  VOLATILE   – extreme VIX                    → scale down, widen stops
  CHOPPY     – no clear direction             → mean-reversion setups only
"""

import numpy as np
import pandas as pd
import yfinance as yf
import logging
from dataclasses import dataclass
from typing import Optional

from scanner.config import CONFIG
from scanner.utils import CACHE, logger

# ── Data class ─────────────────────────────────────────────────────────────────

@dataclass
class MarketRegime:
    classification: str   = "UNKNOWN"   # BULL | BULL_CHOP | BEAR | VOLATILE | CHOPPY

    # Raw inputs
    spy_price:      float = 0.0
    spy_vs_50sma:   float = 0.0    # % above/below
    spy_vs_200sma:  float = 0.0
    spy_trend:      str   = "neutral"  # bullish | bearish | neutral

    vix:            float = 0.0
    vix_regime:     str   = "normal"   # low | normal | high | extreme

    breadth:        float = 0.5    # 0–1 fraction of stocks above 50SMA (proxy)
    advance_decline: float = 0.0   # today's A/D ratio from SPY proxies

    # Sector momentum (strongest / weakest)
    top_sector:     str   = ""
    bot_sector:     str   = ""
    sector_scores:  dict  = None   # {etf: pct_change_1m}

    # Scoring weight adjustments for this regime
    momentum_weight_adj:  float = 1.0
    reversal_weight_adj:  float = 1.0
    min_score_threshold:  float = 60.0

    def is_tradeable(self) -> bool:
        return self.classification not in ("VOLATILE",)

    def summary(self) -> str:
        return (f"Regime={self.classification} | "
                f"SPY vs 50SMA={self.spy_vs_50sma:+.1f}% | "
                f"VIX={self.vix:.1f} ({self.vix_regime}) | "
                f"Top sector={self.top_sector}")


# ── Data fetching ──────────────────────────────────────────────────────────────

def _normalize_column_name(col: object) -> object:
    if isinstance(col, tuple):
        return col[0]
    if isinstance(col, str):
        if col.startswith("('") or col.startswith('(\"'):
            try:
                inner = col[1:-1]
                first_part = inner.split(",", 1)[0].strip()
                return first_part.strip("'\"")
            except Exception:
                return col
    return col


def _normalize_df_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    else:
        df.columns = [_normalize_column_name(col) for col in df.columns]
    return df


def _fetch_ohlcv(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    cache_key = f"regime_{ticker}_{period}"
    cached = CACHE.get(cache_key)
    if cached:
        df = pd.DataFrame(cached)
        return _normalize_df_columns(df)
    try:
        df = yf.download(ticker, period=period, interval="1d",
                         auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty:
            return None
        df = _normalize_df_columns(df)
        CACHE.set(cache_key, df.reset_index().to_dict("records"), ttl=900)
        return df
    except Exception as e:
        logger.warning(f"[Regime] Failed to fetch {ticker}: {e}")
        return None


# ── SPY trend ─────────────────────────────────────────────────────────────────

def _spy_trend(df: pd.DataFrame, lookback: int = 50) -> dict:
    close = df["Close"]
    if len(close) < lookback:
        return {"trend": "neutral", "vs_50": 0.0, "vs_200": 0.0}

    price   = float(close.iloc[-1])
    sma50   = float(close.rolling(50).mean().iloc[-1])
    sma200  = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else np.nan

    vs_50  = (price - sma50)  / sma50  * 100 if sma50  else 0.0
    vs_200 = (price - sma200) / sma200 * 100 if sma200 and not np.isnan(sma200) else 0.0

    # 20-day price slope
    recent = close.iloc[-20:].values
    slope  = float(np.polyfit(range(len(recent)), recent, 1)[0])
    slope_pct = slope / price * 100

    if vs_50 > 1.0 and slope_pct > 0:
        trend = "bullish"
    elif vs_50 < -2.0 or slope_pct < -0.2:
        trend = "bearish"
    else:
        trend = "neutral"

    return {"trend": trend, "vs_50": vs_50, "vs_200": vs_200}


# ── VIX level ─────────────────────────────────────────────────────────────────

def _vix_regime(vix: float) -> str:
    if vix < 15:   return "low"
    if vix < 20:   return "normal"
    if vix < CONFIG.VIX_HIGH:    return "elevated"
    if vix < CONFIG.VIX_EXTREME: return "high"
    return "extreme"


# ── Breadth proxy (% of ETF components above 50SMA) ──────────────────────────

def _breadth_proxy(sector_etfs: list) -> float:
    """
    Quick breadth proxy: fraction of sector ETFs above their 50-day SMA.
    Not a true A/D ratio but good enough for free-data regime detection.
    """
    above = 0
    total = 0
    for etf in sector_etfs[:10]:   # limit to 10 for speed
        df = _fetch_ohlcv(etf, period="3mo")
        if df is None or len(df) < 50:
            continue
        close  = df["Close"]
        sma50  = close.rolling(50).mean().iloc[-1]
        price  = close.iloc[-1]
        above += int(price > sma50)
        total += 1
    return above / total if total else 0.5


# ── Sector momentum ───────────────────────────────────────────────────────────

def _sector_momentum(sector_etfs: list) -> dict:
    """
    Returns dict {etf: 1-month % change} for all sector ETFs.
    """
    scores = {}
    for etf in sector_etfs:
        df = _fetch_ohlcv(etf, period="2mo")
        if df is None or len(df) < 22:
            continue
        close = df["Close"]
        change_1m = (close.iloc[-1] - close.iloc[-22]) / close.iloc[-22] * 100
        scores[etf] = round(float(change_1m), 2)
    return scores


# ── Main regime classifier ────────────────────────────────────────────────────

def get_market_regime() -> MarketRegime:
    """
    Compute and return current MarketRegime.
    Cached for 15 minutes to avoid hammering free APIs.
    """
    cache_key = "market_regime_v2"
    cached = CACHE.get(cache_key)
    if cached:
        # Reconstruct from dict
        r = MarketRegime(**{k: v for k, v in cached.items()
                            if k != "sector_scores"})
        r.sector_scores = cached.get("sector_scores", {})
        logger.info(f"[Regime] (cached) {r.summary()}")
        return r

    regime = MarketRegime()

    # ── SPY ──
    spy_df = _fetch_ohlcv("SPY", period="1y")
    if spy_df is not None and not spy_df.empty:
        spy_data        = _spy_trend(spy_df)
        regime.spy_price   = float(spy_df["Close"].iloc[-1])
        regime.spy_vs_50sma  = spy_data["vs_50"]
        regime.spy_vs_200sma = spy_data["vs_200"]
        regime.spy_trend     = spy_data["trend"]

    # ── VIX ──
    vix_df = _fetch_ohlcv("^VIX", period="1mo")
    if vix_df is not None and not vix_df.empty:
        regime.vix         = float(vix_df["Close"].iloc[-1])
        regime.vix_regime  = _vix_regime(regime.vix)

    # ── Breadth proxy ──
    regime.breadth = _breadth_proxy(CONFIG.SECTOR_ETFS)

    # ── Sector momentum ──
    regime.sector_scores = _sector_momentum(CONFIG.SECTOR_ETFS)
    if regime.sector_scores:
        top = max(regime.sector_scores, key=regime.sector_scores.get)
        bot = min(regime.sector_scores, key=regime.sector_scores.get)
        regime.top_sector = f"{top} ({regime.sector_scores[top]:+.1f}%)"
        regime.bot_sector = f"{bot} ({regime.sector_scores[bot]:+.1f}%)"

    # ── Classification logic ──
    spy_bull  = regime.spy_trend == "bullish"
    spy_bear  = regime.spy_trend == "bearish"
    vix_high  = regime.vix >= CONFIG.VIX_HIGH
    vix_xtrm  = regime.vix >= CONFIG.VIX_EXTREME
    broad_bull = regime.breadth >= 0.60

    if vix_xtrm:
        regime.classification = "VOLATILE"
        regime.momentum_weight_adj  = 0.5
        regime.reversal_weight_adj  = 1.5
        regime.min_score_threshold  = 80.0

    elif spy_bear:
        regime.classification = "BEAR"
        regime.momentum_weight_adj  = 0.7
        regime.reversal_weight_adj  = 1.3
        regime.min_score_threshold  = 70.0

    elif spy_bull and not vix_high and broad_bull:
        regime.classification = "BULL"
        regime.momentum_weight_adj  = 1.3
        regime.reversal_weight_adj  = 0.8
        regime.min_score_threshold  = 55.0

    elif spy_bull and vix_high:
        regime.classification = "BULL_CHOP"
        regime.momentum_weight_adj  = 1.0
        regime.reversal_weight_adj  = 1.0
        regime.min_score_threshold  = 65.0

    else:
        regime.classification = "CHOPPY"
        regime.momentum_weight_adj  = 0.8
        regime.reversal_weight_adj  = 1.2
        regime.min_score_threshold  = 70.0

    logger.info(f"[Regime] {regime.summary()}")
    CACHE.set(cache_key, {
        **regime.__dict__,
        "sector_scores": regime.sector_scores or {},
    }, ttl=900)   # cache 15 min
    return regime
