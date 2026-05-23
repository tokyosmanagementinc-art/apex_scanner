"""
indicators.py  –  Technical Analysis Engine
=============================================
All indicators implemented in pure pandas/numpy.
No TA-Lib dependency.  Each function is stateless and vectorized.

Functions accept pd.Series (or DataFrame for OHLCV) and return pd.Series.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Tuple, Optional


# ── Simple / Exponential Moving Averages ─────────────────────────────────────

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()

def ema_stack(close: pd.Series) -> dict:
    """Return dict of EMA(9), EMA(21), EMA(50) for stack analysis."""
    return {
        "ema9":  ema(close, 9),
        "ema21": ema(close, 21),
        "ema50": ema(close, 50),
    }

def ema_stack_bullish(close: pd.Series) -> bool:
    """EMA9 > EMA21 > EMA50 on most recent bar."""
    stack = ema_stack(close)
    e9  = stack["ema9"].iloc[-1]
    e21 = stack["ema21"].iloc[-1]
    e50 = stack["ema50"].iloc[-1]
    return e9 > e21 > e50 and not np.isnan(e50)


# ── RSI ───────────────────────────────────────────────────────────────────────

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta  = close.diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_l  = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


# ── MACD ──────────────────────────────────────────────────────────────────────

def macd(close: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    fast_ema   = ema(close, fast)
    slow_ema   = ema(close, slow)
    macd_line  = fast_ema - slow_ema
    sig_line   = ema(macd_line, signal)
    histogram  = macd_line - sig_line
    return macd_line, sig_line, histogram


# ── Bollinger Bands ───────────────────────────────────────────────────────────

def bollinger_bands(close: pd.Series, period: int = 20,
                    std_dev: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper, mid, lower)."""
    mid   = sma(close, period)
    sigma = close.rolling(period).std()
    upper = mid + std_dev * sigma
    lower = mid - std_dev * sigma
    return upper, mid, lower

def bb_pct_b(close: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.Series:
    """Position within Bollinger Bands: 0 = lower, 1 = upper, 0.5 = mid."""
    upper, _, lower = bollinger_bands(close, period, std_dev)
    return (close - lower) / (upper - lower).replace(0, np.nan)

def bb_squeeze(close: pd.Series, period: int = 20, std_dev: float = 2.0,
               squeeze_pct: float = 0.03) -> pd.Series:
    """True where band width / mid < squeeze_pct  (compression before breakout)."""
    upper, mid, lower = bollinger_bands(close, period, std_dev)
    width = (upper - lower) / mid.replace(0, np.nan)
    return width < squeeze_pct


# ── ATR ───────────────────────────────────────────────────────────────────────

def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


# ── ADX (Average Directional Index) ──────────────────────────────────────────

def adx(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (adx, +DI, -DI)."""
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    plus_dm  = (high - prev_high).clip(lower=0)
    minus_dm = (prev_low - low).clip(lower=0)
    # Remove when both are equal
    equal    = (plus_dm == minus_dm)
    plus_dm[equal]  = 0
    minus_dm[equal] = 0

    tr_val = atr(high, low, close, period=1)
    atr_val = tr_val.ewm(alpha=1 / period, adjust=False).mean()

    plus_di  = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_val.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_val.replace(0, np.nan))

    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx_val = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx_val, plus_di, minus_di


# ── Stochastic RSI ────────────────────────────────────────────────────────────

def stoch_rsi(close: pd.Series, rsi_period: int = 14, stoch_k: int = 3,
              stoch_d: int = 3) -> Tuple[pd.Series, pd.Series]:
    """Returns (%K, %D) stochastic oscillator of RSI."""
    rsi_vals  = rsi(close, rsi_period)
    rsi_min   = rsi_vals.rolling(rsi_period).min()
    rsi_max   = rsi_vals.rolling(rsi_period).max()
    stoch     = 100 * (rsi_vals - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    k_line    = stoch.rolling(stoch_k).mean()
    d_line    = k_line.rolling(stoch_d).mean()
    return k_line, d_line


# ── VWAP ──────────────────────────────────────────────────────────────────────

def vwap(high: pd.Series, low: pd.Series, close: pd.Series,
         volume: pd.Series) -> pd.Series:
    """Session VWAP (resets at start of series – pass intraday data)."""
    typical  = (high + low + close) / 3.0
    cum_tpv  = (typical * volume).cumsum()
    cum_vol  = volume.cumsum()
    return cum_tpv / cum_vol.replace(0, np.nan)

def anchored_vwap(high: pd.Series, low: pd.Series, close: pd.Series,
                  volume: pd.Series, anchor_idx: int = 0) -> pd.Series:
    """VWAP anchored to a specific bar index (e.g. major low)."""
    h = high.iloc[anchor_idx:]
    l = low.iloc[anchor_idx:]
    c = close.iloc[anchor_idx:]
    v = volume.iloc[anchor_idx:]
    return vwap(h, l, c, v)


# ── Relative Volume ───────────────────────────────────────────────────────────

def relative_volume(volume: pd.Series, period: int = 20) -> pd.Series:
    avg = volume.rolling(period, min_periods=max(1, period // 2)).mean()
    return volume / avg.replace(0, np.nan)


# ── Volume Profile (simplified) ───────────────────────────────────────────────

def volume_nodes(close: pd.Series, volume: pd.Series,
                 bins: int = 30) -> Tuple[np.ndarray, np.ndarray]:
    """
    Returns (price_levels, volumes) histogram.
    High-volume nodes = key S/R levels.
    """
    hist, edges = np.histogram(close.dropna(), bins=bins,
                               weights=volume.dropna())
    centers = (edges[:-1] + edges[1:]) / 2
    return centers, hist


# ── Gap Detection ─────────────────────────────────────────────────────────────

def gap_pct(open_: pd.Series, prev_close: pd.Series) -> pd.Series:
    """Overnight gap as % of previous close."""
    return (open_ - prev_close) / prev_close.replace(0, np.nan) * 100


# ── Opening Range Breakout ────────────────────────────────────────────────────

def opening_range(high: pd.Series, low: pd.Series,
                  close: pd.Series, lookback: int = 6) -> Tuple[float, float]:
    """
    Returns (opening_range_high, opening_range_low).
    Use the first `lookback` bars of intraday data.
    """
    orh = high.iloc[:lookback].max()
    orl = low.iloc[:lookback].min()
    return float(orh), float(orl)


# ── Trend Detection ───────────────────────────────────────────────────────────

def higher_highs_higher_lows(high: pd.Series, low: pd.Series,
                              lookback: int = 10) -> bool:
    """Returns True if last `lookback` bars show HH+HL structure."""
    h = high.iloc[-lookback:]
    l = low.iloc[-lookback:]
    highs_up = all(h.iloc[i] >= h.iloc[i-1] for i in range(1, len(h)))
    lows_up  = all(l.iloc[i] >= l.iloc[i-1] for i in range(1, len(l)))
    return highs_up and lows_up

def trend_direction(close: pd.Series,
                    fast: int = 21, slow: int = 50) -> str:
    """'bullish' | 'bearish' | 'neutral'"""
    if len(close) < slow:
        return "neutral"
    e_fast = ema(close, fast).iloc[-1]
    e_slow = ema(close, slow).iloc[-1]
    diff_pct = (e_fast - e_slow) / e_slow * 100
    if diff_pct >  0.5:
        return "bullish"
    if diff_pct < -0.5:
        return "bearish"
    return "neutral"


# ── Squeeze Momentum (simplified John Carter style) ──────────────────────────

def squeeze_momentum(close: pd.Series, high: pd.Series, low: pd.Series,
                     bb_period: int = 20, kc_mult: float = 1.5) -> pd.Series:
    """
    Returns momentum histogram values.
    Positive = bullish momentum; negative = bearish.
    """
    atr_val = atr(high, low, close, period=bb_period)
    mid     = sma(close, bb_period)
    kc_upper = mid + kc_mult * atr_val
    kc_lower = mid - kc_mult * atr_val

    upper_bb, _, lower_bb = bollinger_bands(close, bb_period)

    # Squeeze: BB inside KC
    # squeeze_on = (lower_bb > kc_lower) & (upper_bb < kc_upper)

    # Momentum: delta of close vs midpoint of high/low/SMA
    highest_high = high.rolling(bb_period).max()
    lowest_low   = low.rolling(bb_period).min()
    midline      = (highest_high + lowest_low) / 2
    delta        = close - (midline + mid) / 2

    momentum     = pd.Series(
        np.polyval(np.polyfit(range(len(delta.dropna())), delta.dropna(), 1),
                   range(len(delta.dropna()))),
        index=delta.dropna().index,
    ) if len(delta.dropna()) > 2 else delta

    return momentum


# ── Short Squeeze Probability ─────────────────────────────────────────────────

def short_squeeze_score(short_float_pct: float, days_to_cover: float,
                        rel_vol: float, change_pct: float) -> float:
    """
    0-100 score for squeeze probability.
    Inputs:
      short_float_pct : float 0–100  (% of float short)
      days_to_cover   : float         (short interest / avg daily volume)
      rel_vol         : float         (today vol / avg vol)
      change_pct      : float         (today's % change)
    """
    score = 0.0
    # High short interest
    if short_float_pct > 30: score += 30
    elif short_float_pct > 20: score += 20
    elif short_float_pct > 10: score += 10

    # Tight covering window
    if days_to_cover > 5: score += 25
    elif days_to_cover > 3: score += 15
    elif days_to_cover > 1: score += 5

    # Volume surge
    score += min(rel_vol * 8, 25)

    # Positive price pressure
    score += min(change_pct * 1.5, 20)

    return min(score, 100.0)


# ── Composite signals dataclass ───────────────────────────────────────────────

@dataclass
class IndicatorBundle:
    """All computed indicator values for one symbol."""
    symbol:           str     = ""

    # Trend
    ema9:             float   = np.nan
    ema21:            float   = np.nan
    ema50:            float   = np.nan
    ema_stack_bull:   bool    = False
    trend_dir:        str     = "neutral"

    # Momentum
    rsi_val:          float   = 50.0
    rsi_slope:        float   = 0.0    # 5-bar slope
    macd_hist:        float   = 0.0
    macd_cross_up:    bool    = False

    # Volume
    rel_vol:          float   = 1.0
    vwap_val:         float   = np.nan
    price_vs_vwap:    float   = 0.0    # % above/below VWAP

    # Volatility / Range
    atr_val:          float   = np.nan
    atr_pct:          float   = 0.0   # ATR / price %
    bb_pct_b_val:     float   = 0.5
    bb_squeeze_on:    bool    = False

    # ADX / Strength
    adx_val:          float   = 0.0
    plus_di:          float   = 0.0
    minus_di:         float   = 0.0

    # StochRSI
    stoch_k:          float   = 50.0
    stoch_d:          float   = 50.0

    # Breakout
    breakout_signal:  str     = "none"    # 'up', 'down', 'none'
    squeeze_firing:   bool    = False

    # Short squeeze
    squeeze_score:    float   = 0.0


def compute_indicators(symbol: str,
                       df_daily: pd.DataFrame,
                       df_intra: Optional[pd.DataFrame] = None,
                       short_float: float = 0.0,
                       days_to_cover: float = 0.0) -> IndicatorBundle:
    """
    Compute all indicators from daily OHLCV DataFrame.
    Optional intraday DataFrame for VWAP.
    DataFrame must have columns: Open, High, Low, Close, Volume.
    """
    b = IndicatorBundle(symbol=symbol)

    if df_daily is None or len(df_daily) < 10:
        return b

    close  = df_daily["Close"]
    high   = df_daily["High"]
    low    = df_daily["Low"]
    volume = df_daily["Volume"]
    open_  = df_daily["Open"] if "Open" in df_daily.columns else close

    def last(s) -> float:
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0] if s.shape[1] == 1 else s.iloc[:, -1]
        v = s.iloc[-1]
        if isinstance(v, pd.Series):
            v = v.iloc[-1]
        if isinstance(v, np.generic):
            v = v.item()
        return float(v) if not (pd.isna(v) or np.isinf(v)) else np.nan

    # EMA stack
    e9, e21, e50 = ema(close, 9), ema(close, 21), ema(close, 50)
    b.ema9, b.ema21, b.ema50 = last(e9), last(e21), last(e50)
    b.ema_stack_bull = (b.ema9 > b.ema21 > b.ema50
                        and not any(np.isnan([b.ema9, b.ema21, b.ema50])))
    b.trend_dir = trend_direction(close)

    # RSI
    rsi_series  = rsi(close, 14)
    b.rsi_val   = last(rsi_series)
    rsi_5       = rsi_series.iloc[-6:].dropna()
    if len(rsi_5) >= 2:
        b.rsi_slope = float(np.polyfit(range(len(rsi_5)), rsi_5.values, 1)[0])

    # MACD
    ml, sl, hist = macd(close)
    b.macd_hist  = last(hist)
    if len(hist.dropna()) >= 2:
        b.macd_cross_up = (hist.iloc[-1] > 0 and hist.iloc[-2] <= 0)

    # Relative volume
    rvol         = relative_volume(volume)
    b.rel_vol    = last(rvol)

    # VWAP – use intraday if available, else approximate from daily
    source = df_intra if (df_intra is not None and len(df_intra) > 0) else df_daily
    vwap_s  = vwap(source["High"], source["Low"], source["Close"], source["Volume"])
    b.vwap_val  = last(vwap_s)
    if b.vwap_val and not np.isnan(b.vwap_val):
        b.price_vs_vwap = (last(close) - b.vwap_val) / b.vwap_val * 100

    # ATR
    atr_s  = atr(high, low, close)
    b.atr_val  = last(atr_s)
    if b.atr_val and last(close):
        b.atr_pct = b.atr_val / last(close) * 100

    # Bollinger Bands
    b.bb_pct_b_val   = last(bb_pct_b(close))
    squeeze_s        = bb_squeeze(close)
    b.bb_squeeze_on  = bool(squeeze_s.iloc[-1]) if len(squeeze_s) > 0 else False

    # ADX
    adx_s, pdi, mdi = adx(high, low, close)
    b.adx_val   = last(adx_s)
    b.plus_di   = last(pdi)
    b.minus_di  = last(mdi)

    # StochRSI
    sk, sd    = stoch_rsi(close)
    b.stoch_k = last(sk)
    b.stoch_d = last(sd)

    # Breakout detection: close above recent resistance
    high_20 = high.rolling(20).max().iloc[-2]   # prior 20-bar high (exclude today)
    if last(close) > high_20:
        b.breakout_signal = "up"
    low_20 = low.rolling(20).min().iloc[-2]
    if last(close) < low_20:
        b.breakout_signal = "down"

    # Squeeze firing: was in BB squeeze, now breaking out
    if len(squeeze_s) >= 2:
        b.squeeze_firing = bool(squeeze_s.iloc[-2] and not squeeze_s.iloc[-1])

    # Short squeeze score
    b.squeeze_score = short_squeeze_score(
        short_float_pct=short_float,
        days_to_cover=days_to_cover,
        rel_vol=b.rel_vol if not np.isnan(b.rel_vol) else 1.0,
        change_pct=float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100)
                   if len(close) >= 2 else 0.0,
    )

    return b
