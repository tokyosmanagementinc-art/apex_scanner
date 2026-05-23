"""
support_resistance.py  –  Support & Resistance Engine
======================================================
Detects key price levels from multiple sources:
  • Pivot highs / lows   (fractal method)
  • Volume nodes         (high-volume price zones)
  • Anchored VWAP        (from significant swing points)
  • Previous day / week highs & lows
  • Round number magnets ($5, $10, $50 … )
  • Trendline projection (linear regression channel)
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

from scanner.utils import logger


# ── Data class ─────────────────────────────────────────────────────────────────

@dataclass
class SRLevel:
    price:    float
    type:     str           # 'support' | 'resistance' | 'pivot' | 'vwap' | 'round'
    strength: float  = 0.0  # 0–1 normalised
    source:   str    = ""
    touches:  int    = 0    # how many times price approached this level


@dataclass
class SRBundle:
    symbol:        str
    current_price: float
    levels:        List[SRLevel] = field(default_factory=list)

    # Nearest actionable levels relative to current price
    nearest_support:    Optional[float] = None
    nearest_resistance: Optional[float] = None
    prev_day_high:      Optional[float] = None
    prev_day_low:       Optional[float] = None
    prev_week_high:     Optional[float] = None
    prev_week_low:      Optional[float] = None
    vwap:               Optional[float] = None

    def support_distance_pct(self) -> float:
        if self.nearest_support and self.current_price:
            return (self.current_price - self.nearest_support) / self.current_price * 100
        return 0.0

    def resistance_distance_pct(self) -> float:
        if self.nearest_resistance and self.current_price:
            return (self.nearest_resistance - self.current_price) / self.current_price * 100
        return 0.0

    def near_resistance(self, threshold_pct: float = 1.0) -> bool:
        return 0 <= self.resistance_distance_pct() <= threshold_pct

    def near_support(self, threshold_pct: float = 1.0) -> bool:
        return 0 <= self.support_distance_pct() <= threshold_pct


# ── Pivot highs / lows ────────────────────────────────────────────────────────

def find_pivot_highs(high: pd.Series, window: int = 5) -> List[Tuple[int, float]]:
    """Returns list of (index, price) for pivot highs."""
    pivots = []
    arr = high.values
    for i in range(window, len(arr) - window):
        if arr[i] == arr[i-window:i+window+1].max():
            pivots.append((i, arr[i]))
    return pivots

def find_pivot_lows(low: pd.Series, window: int = 5) -> List[Tuple[int, float]]:
    """Returns list of (index, price) for pivot lows."""
    pivots = []
    arr = low.values
    for i in range(window, len(arr) - window):
        if arr[i] == arr[i-window:i+window+1].min():
            pivots.append((i, arr[i]))
    return pivots


# ── Cluster levels  ───────────────────────────────────────────────────────────

def cluster_levels(prices: List[float], tolerance_pct: float = 0.5) -> List[Tuple[float, int]]:
    """
    Group nearby price levels into clusters.
    Returns list of (cluster_centre, count).
    """
    if not prices:
        return []
    prices_sorted = sorted(prices)
    clusters: List[List[float]] = []

    for p in prices_sorted:
        placed = False
        for cluster in clusters:
            centre = sum(cluster) / len(cluster)
            if abs(p - centre) / centre * 100 <= tolerance_pct:
                cluster.append(p)
                placed = True
                break
        if not placed:
            clusters.append([p])

    return [(sum(c) / len(c), len(c)) for c in clusters]


# ── Volume nodes ──────────────────────────────────────────────────────────────

def volume_node_levels(close: pd.Series, volume: pd.Series,
                       bins: int = 40, top_n: int = 6) -> List[float]:
    """Return price levels of the top-N highest-volume nodes."""
    if len(close) < 10:
        return []
    hist, edges = np.histogram(close.values, bins=bins,
                               weights=volume.values)
    centers = (edges[:-1] + edges[1:]) / 2
    top_idx = np.argsort(hist)[-top_n:]
    return [float(centers[i]) for i in top_idx]


# ── Round numbers ─────────────────────────────────────────────────────────────

def round_number_levels(price: float, band: float = 0.10) -> List[float]:
    """
    Returns nearby round-number price magnets.
    E.g. for price=47.80: [45, 47.5, 50] depending on magnitude.
    """
    levels = []
    mag = 10 ** int(np.log10(price))   # 1, 10, 100 …
    step = max(mag / 10, 0.5)
    base = round(price / step) * step
    for n in range(-3, 4):
        candidate = round(base + n * step, 4)
        if abs(candidate - price) / price <= band:
            levels.append(candidate)
    return levels


# ── Trendline (linear regression channel) ────────────────────────────────────

def lr_channel(close: pd.Series,
               lookback: int = 50) -> Tuple[float, float, float]:
    """
    Returns (regression_price, upper_channel, lower_channel) for last bar.
    Uses 2-sigma channel.
    """
    sub = close.iloc[-lookback:]
    x   = np.arange(len(sub))
    if len(sub) < 10:
        p = float(sub.iloc[-1])
        return p, p, p
    coeffs  = np.polyfit(x, sub.values, 1)
    fitted  = np.polyval(coeffs, x)
    resid   = sub.values - fitted
    sigma   = float(np.std(resid))
    mid     = float(np.polyval(coeffs, x[-1]))
    return mid, mid + 2 * sigma, mid - 2 * sigma


# ── Previous period levels ────────────────────────────────────────────────────

def previous_day_levels(df: pd.DataFrame) -> Tuple[float, float]:
    """Returns (prev_day_high, prev_day_low) if available."""
    if len(df) < 2:
        return float("nan"), float("nan")
    prev = df.iloc[-2]
    return float(prev["High"]), float(prev["Low"])

def previous_week_levels(df: pd.DataFrame) -> Tuple[float, float]:
    """Returns (prev_week_high, prev_week_low) using last 5 bars."""
    if len(df) < 6:
        return float("nan"), float("nan")
    week = df.iloc[-6:-1]
    return float(week["High"].max()), float(week["Low"].min())


# ── VWAP level ────────────────────────────────────────────────────────────────

def session_vwap(df: pd.DataFrame) -> float:
    """Approximate session VWAP from daily data (use intraday for precision)."""
    if len(df) < 1:
        return float("nan")
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    today   = df.iloc[-1]
    return float((today["High"] + today["Low"] + today["Close"]) / 3)   # daily approx


# ── Master function ───────────────────────────────────────────────────────────

def compute_sr(symbol: str, df: pd.DataFrame,
               pivot_window: int = 5,
               tolerance_pct: float = 0.5) -> SRBundle:
    """
    Compute all support/resistance levels for `symbol` from OHLCV DataFrame.
    Returns SRBundle with nearest_support, nearest_resistance and all levels.
    """
    bundle = SRBundle(symbol=symbol, current_price=float(df["Close"].iloc[-1]))
    price  = bundle.current_price

    all_sr_prices: List[float] = []

    # 1. Pivot highs → resistance candidates
    ph = find_pivot_highs(df["High"], window=pivot_window)
    for _, p in ph[-20:]:   # last 20 pivots
        all_sr_prices.append(p)

    # 2. Pivot lows → support candidates
    pl = find_pivot_lows(df["Low"], window=pivot_window)
    for _, p in pl[-20:]:
        all_sr_prices.append(p)

    # 3. Volume nodes
    vn = volume_node_levels(df["Close"], df["Volume"])
    all_sr_prices.extend(vn)

    # 4. Round numbers
    rn = round_number_levels(price, band=0.12)
    all_sr_prices.extend(rn)

    # 5. LR Channel bounds
    _, ch_upper, ch_lower = lr_channel(df["Close"])
    all_sr_prices.extend([ch_upper, ch_lower])

    # 6. Previous day / week
    pdh, pdl = previous_day_levels(df)
    pwh, pwl = previous_week_levels(df)
    bundle.prev_day_high  = pdh if not np.isnan(pdh) else None
    bundle.prev_day_low   = pdl if not np.isnan(pdl) else None
    bundle.prev_week_high = pwh if not np.isnan(pwh) else None
    bundle.prev_week_low  = pwl if not np.isnan(pwl) else None
    for p in [pdh, pdl, pwh, pwl]:
        if not np.isnan(p):
            all_sr_prices.append(p)

    # 7. VWAP
    bundle.vwap = session_vwap(df)
    if bundle.vwap:
        all_sr_prices.append(bundle.vwap)

    # ── Cluster & build SRLevel objects ──
    clustered = cluster_levels(all_sr_prices, tolerance_pct=tolerance_pct)
    for centre, count in clustered:
        strength = min(count / 5, 1.0)
        sr_type  = "resistance" if centre > price else "support"
        bundle.levels.append(SRLevel(
            price=round(centre, 4),
            type=sr_type,
            strength=strength,
            source="cluster",
            touches=count,
        ))

    # ── Find nearest support below and resistance above ──
    supports   = [l.price for l in bundle.levels if l.price < price]
    resistances = [l.price for l in bundle.levels if l.price > price]

    bundle.nearest_support    = max(supports,    default=None)
    bundle.nearest_resistance = min(resistances, default=None)

    # Sort levels
    bundle.levels.sort(key=lambda l: l.price)

    return bundle
