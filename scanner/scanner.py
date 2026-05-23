"""
scanner.py  –  Main Orchestration Engine
==========================================
Wires together every pipeline stage:

  Universe Discovery
      ↓
  Market Regime
      ↓
  Deep Technical Analysis  (concurrent, yfinance batch)
      ↓
  Relative Strength vs SPY / Sector
      ↓
  Support & Resistance
      ↓
  Scoring
      ↓
  Risk / Trade Plan
      ↓
  Alerts + DB persist + Display

All data is fetched live (no preset watchlist).
"""

import time
import math
import logging
import threading
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from typing import Callable, List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from scanner.config import CONFIG
from scanner.utils import CACHE, Timer, chunk_list, fmt_price, fmt_pct, fmt_vol, logger, retry
from scanner.universe import UniverseStock, UniverseStats, discover_universe
from scanner.indicators import IndicatorBundle, compute_indicators
from scanner.market_regime import MarketRegime, get_market_regime
from scanner.support_resistance import SRBundle, compute_sr
from scanner.scoring import SignalScore, ScoreComponent, compute_score, rank_signals
from scanner.risk import TradePlan, PortfolioRisk, build_trade_plan
from scanner.alerts import send_alert
from scanner.database import DB


# ── Result container ───────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    stock:   UniverseStock
    ind:     IndicatorBundle
    sr:      Optional[SRBundle]
    signal:  SignalScore
    plan:    TradePlan
    price:   float
    sector:  str = ""

    def one_liner(self) -> str:
        arrow = "▲" if self.signal.direction == "LONG" else "▼"
        return (f"  {arrow} {self.stock.symbol:<6}  "
                f"Score={self.signal.composite:5.1f}  "
                f"Conf={self.signal.confidence:<9}  "
                f"Price={fmt_price(self.price):<10}  "
                f"Entry={fmt_price(self.plan.entry):<10}  "
                f"SL={fmt_price(self.plan.stop_loss):<10}  "
                f"TP2={fmt_price(self.plan.tp2):<10}  "
                f"R/R={self.plan.rr_ratio:<4.1f}  "
                f"RVol={self.stock.rel_volume:.1f}x  "
                f"Flags={','.join(self.signal.flags) or '-'}")


# ── Deep analysis for one candidate ──────────────────────────────────────────

def _normalize_yf_history_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    if not df.columns.is_unique:
        df = df.loc[:, ~df.columns.duplicated()]
    return df


def _is_yf_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "too many requests" in msg
        or "rate limit" in msg
        or "yfratelimiterror" in type(exc).__name__.lower()
        or "429" in msg
    )


@retry(max_tries=3, delay=2.5, backoff=2.0, exceptions=(Exception,))
def _download_yf_history(symbol: str, period: str) -> pd.DataFrame:
    return yf.download(symbol, period=period, interval="1d",
                       auto_adjust=True, progress=False, threads=False)


@retry(max_tries=3, delay=2.5, backoff=2.0, exceptions=(Exception,))
def _download_fast_info(symbol: str):
    return yf.Ticker(symbol).fast_info


def _scan_cancelled(cancel_event: Optional[threading.Event]) -> bool:
    return cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)()


@retry(max_tries=2, delay=5.0, backoff=2.0, exceptions=(Exception,))
def _download_av_daily_history(symbol: str) -> pd.DataFrame:
    if not CONFIG.ALPHA_VANTAGE_KEY:
        raise Exception("No Alpha Vantage key configured")
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "TIME_SERIES_DAILY_ADJUSTED",
        "symbol": symbol,
        "outputsize": "compact",
        "apikey": CONFIG.ALPHA_VANTAGE_KEY,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if "Time Series (Daily)" not in data:
        raise Exception("Alpha Vantage history missing data")
    series = data["Time Series (Daily)"]
    df = pd.DataFrame.from_dict(series, orient="index")
    df = df.rename(columns={
        "1. open": "Open",
        "2. high": "High",
        "3. low": "Low",
        "4. close": "Close",
        "5. adjusted close": "Adj Close",
        "6. volume": "Volume",
    })
    df = df[[("Open"), ("High"), ("Low"), ("Close"), ("Volume")]]
    df = df.astype({"Open": float, "High": float, "Low": float, "Close": float, "Volume": int})
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    return df


@retry(max_tries=2, delay=5.0, backoff=2.0, exceptions=(Exception,))
def _download_finnhub_profile(symbol: str) -> dict:
    if not CONFIG.FINNHUB_KEY:
        raise Exception("No Finnhub key configured")
    url = "https://finnhub.io/api/v1/stock/profile2"
    params = {"symbol": symbol, "token": CONFIG.FINNHUB_KEY}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _get_spy_change() -> float:
    """Current SPY daily % change."""
    cache_key = "spy_change_today"
    v = CACHE.get(cache_key)
    if v is not None:
        return float(v)
    try:
        df = _download_yf_history("SPY", period="2d")
        df = _normalize_yf_history_df(df)
        if df is not None and len(df) >= 2:
            val = float((df["Close"].iloc[-1] - df["Close"].iloc[-2]) /
                        df["Close"].iloc[-2] * 100)
            CACHE.set(cache_key, val, ttl=300)
            return val
    except Exception as e:
        if _is_yf_rate_limit_error(e):
            logger.warning(f"[Scanner] SPY download rate-limited: {e}")
        else:
            logger.debug(f"[Scanner] Failed to fetch SPY change: {e}")
    return 0.0


def _fetch_history(symbol: str, period: str = "3mo") -> Optional[pd.DataFrame]:
    cache_key = f"hist_{symbol}_{period}"
    cached = CACHE.get(cache_key)
    if cached:
        df = pd.DataFrame(cached)
        if "Date" in df.columns:
            df = df.set_index("Date")
        return _normalize_yf_history_df(df)
    try:
        df = _download_yf_history(symbol, period=period)
        df = _normalize_yf_history_df(df)
        if df is None or df.empty:
            raise Exception("yfinance returned empty history")
        CACHE.set(cache_key, df.reset_index().to_dict("records"), ttl=CONFIG.CACHE_TTL_SECS)
        return df
    except Exception as e:
        if _is_yf_rate_limit_error(e):
            logger.warning(f"[Scanner] History download rate-limited for {symbol}: {e}")
        else:
            logger.debug(f"[Scanner] yfinance history failed for {symbol}: {e}")

    if CONFIG.ALPHA_VANTAGE_KEY and period in ("3mo", "6mo", "1y"):
        try:
            df = _download_av_daily_history(symbol)
            df = _normalize_yf_history_df(df)
            if df is not None and not df.empty:
                CACHE.set(cache_key, df.reset_index().to_dict("records"), ttl=CONFIG.CACHE_TTL_SECS)
                return df
        except Exception as e:
            logger.warning(f"[Scanner] AlphaVantage history failed for {symbol}: {e}")

    return None


def _fetch_ticker_info(symbol: str) -> dict:
    cache_key = f"info_{symbol}"
    cached = CACHE.get(cache_key)
    if cached:
        return cached
    try:
        info = _download_fast_info(symbol)
        sector = getattr(info, "sector", "") or ""
        market_cap = getattr(info, "market_cap", 0.0) or 0.0
        float_sh = getattr(info, "shares_outstanding", 0.0) or 0.0
        result = {"sector": sector, "market_cap": market_cap, "float_shares": float_sh}
        CACHE.set(cache_key, result, ttl=CONFIG.CACHE_TTL_SECS * 6)
        return result
    except Exception as e:
        if _is_yf_rate_limit_error(e):
            logger.warning(f"[Scanner] fast_info rate-limited for {symbol}: {e}")
        else:
            logger.debug(f"[Scanner] fast_info failed for {symbol}: {e}")

    if CONFIG.FINNHUB_KEY:
        try:
            info = _download_finnhub_profile(symbol)
            sector = info.get("finnhubIndustry", "") or info.get("industry", "") or ""
            market_cap = float(info.get("marketCapitalization") or 0.0)
            float_sh = float(info.get("shareOutstanding") or 0.0)
            result = {"sector": sector, "market_cap": market_cap, "float_shares": float_sh}
            CACHE.set(cache_key, result, ttl=CONFIG.CACHE_TTL_SECS * 6)
            return result
        except Exception as e:
            logger.warning(f"[Scanner] Finnhub profile failed for {symbol}: {e}")

    if CONFIG.ALPHA_VANTAGE_KEY:
        try:
            url = "https://www.alphavantage.co/query"
            params = {
                "function": "OVERVIEW",
                "symbol": symbol,
                "apikey": CONFIG.ALPHA_VANTAGE_KEY,
            }
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            sector = data.get("Sector", "") or ""
            market_cap = float(data.get("MarketCapitalization") or 0.0)
            float_sh = float(data.get("SharesOutstanding") or 0.0)
            result = {"sector": sector, "market_cap": market_cap, "float_shares": float_sh}
            CACHE.set(cache_key, result, ttl=CONFIG.CACHE_TTL_SECS * 6)
            return result
        except Exception as e:
            logger.warning(f"[Scanner] AlphaVantage overview failed for {symbol}: {e}")

    return {"sector": "", "market_cap": 0.0, "float_shares": 0.0}


def _sector_change(sector: str, regime: MarketRegime) -> float:
    """Return 1-day change for the sector ETF mapped from sector name."""
    _map = {
        "Technology": "XLK", "Financial Services": "XLF",
        "Energy": "XLE", "Healthcare": "XLV", "Industrials": "XLI",
        "Consumer Defensive": "XLP", "Utilities": "XLU",
        "Basic Materials": "XLB", "Communication Services": "XLC",
        "Real Estate": "XLRE", "Semiconductor": "SMH", "Biotechnology": "XBI",
    }
    etf = _map.get(sector)
    if not etf or not regime.sector_scores:
        return 0.0
    # sector_scores holds 1-month returns; approximate 1-day as /22
    return regime.sector_scores.get(etf, 0.0) / 22


def analyze_candidate(stock: UniverseStock,
                       regime: MarketRegime,
                       spy_change: float,
                       portfolio: Optional[PortfolioRisk],
                       market_session: str = "regular") -> Optional[ScanResult]:
    """
    Full deep analysis for one candidate.
    Returns ScanResult or None if the symbol is not worth trading.
    """
    sym = stock.symbol

    # 1. Fetch 3-month daily history
    df = _fetch_history(sym, period="3mo")
    if df is None or len(df) < 20:
        return None

    # 2. Fetch ticker metadata
    info   = _fetch_ticker_info(sym)
    sector = info.get("sector", "")

    # 3. Compute all indicators
    ind = compute_indicators(sym, df,
                              short_float=stock.short_pct,
                              days_to_cover=0.0)

    # 4. Support & resistance
    sr = compute_sr(sym, df)

    # 5. Score
    price_change  = stock.change_pct
    sec_change    = _sector_change(sector, regime)

    sig = compute_score(
        symbol        = sym,
        ind           = ind,
        sr            = sr,
        regime        = regime,
        price_change  = price_change,
        sector        = sector,
        spy_change    = spy_change,
        sector_change = sec_change,
        market_session=market_session,
    )

    # Filter below min threshold for regime
    if sig.composite < regime.min_score_threshold:
        return None

    # 6. Trade plan
    price = stock.price
    plan  = build_trade_plan(sig, ind, sr, price, CONFIG, portfolio, sector)

    if not plan.valid:
        logger.debug(f"[Scanner] {sym} rejected by risk: {plan.reject_reason}")
        return None

    return ScanResult(
        stock  = stock,
        ind    = ind,
        sr     = sr,
        signal = sig,
        plan   = plan,
        price  = price,
        sector = sector,
    )


# ── Stage 3: concurrent deep analysis ────────────────────────────────────────

def run_deep_analysis(candidates: List[UniverseStock],
                      regime: MarketRegime,
                      cancel_event: Optional[threading.Event] = None,
                      market_session: str = "regular",
                      max_workers: int = 10,
                      result_callback: Optional[Callable[[ScanResult], None]] = None,
                      progress_callback: Optional[Callable[[int, int], None]] = None) -> List[ScanResult]:
    spy_change = _get_spy_change()
    portfolio  = PortfolioRisk(
        account_size      = CONFIG.ACCOUNT_SIZE,
        daily_loss_budget = CONFIG.ACCOUNT_SIZE * CONFIG.MAX_RISK_PER_TRADE_PCT * 5,
    )

    results: List[ScanResult] = []
    total = len(candidates)
    processed = 0
    logger.info(f"[Stage3] Deep analysis on {total} candidates …")

    if _scan_cancelled(cancel_event):
        raise Exception("Scan canceled")

    workers = min(4, max(1, max_workers))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(analyze_candidate, s, regime, spy_change, portfolio, market_session): s
                for s in candidates
            }
            for fut in as_completed(futures):
                if _scan_cancelled(cancel_event):
                    logger.info("[Stage3] Cancel requested, stopping deep analysis early")
                    break
                processed += 1
                try:
                    r = fut.result()
                    if r:
                        results.append(r)
                        if result_callback:
                            result_callback(r)
                except Exception as e:
                    stock = futures[fut]
                    logger.debug(f"[Stage3] {stock.symbol} analysis error: {e}")
                finally:
                    if progress_callback:
                        progress_callback(processed, total)
    except RuntimeError as e:
        logger.warning(f"[Stage3] Thread pool creation failed: {e}. Running deep analysis serially.")
        for s in candidates:
            if _scan_cancelled(cancel_event):
                raise Exception("Scan canceled")
            try:
                r = analyze_candidate(s, regime, spy_change, portfolio, market_session)
                if r:
                    results.append(r)
            except Exception as e:
                logger.debug(f"[Stage3] {s.symbol} analysis error: {e}")

    if _scan_cancelled(cancel_event):
        raise Exception("Scan canceled")

    # Sort by composite score
    results.sort(key=lambda r: r.signal.composite, reverse=True)
    logger.info(f"[Stage3] {len(results)} valid setups found")
    return results


# ── Full scan orchestration ───────────────────────────────────────────────────

def run_full_scan(force_universe_refresh: bool = False,
                  market_session: str = "regular",
                  cancel_event: Optional[threading.Event] = None,
                  result_callback: Optional[Callable[[ScanResult], None]] = None,
                  progress_callback: Optional[Callable[[int, int], None]] = None,
                  stage_callback: Optional[Callable[[UniverseStats], None]] = None) -> Tuple[List[ScanResult], MarketRegime, UniverseStats]:
    """
    Execute the complete scanning pipeline from raw universe to trade plans.
    Returns (results, regime, stats).
    """
    scan_start = time.time()
    logger.info("=" * 60)
    logger.info("  APEX SCANNER  –  Full scan starting")
    logger.info(f"  Session: {market_session}")
    logger.info("=" * 60)

    # ── 1. Market regime ──
    with Timer("Market Regime"):
        regime = get_market_regime()
    logger.info(f"[Main] {regime.summary()}")

    if not regime.is_tradeable():
        logger.warning(f"[Main] Market regime {regime.classification} – extreme caution")

    # ── 2. Dynamic universe (NO preset watchlist) ──
    with Timer("Universe Discovery"):
        candidates, stats = discover_universe(
            force_refresh=force_universe_refresh,
            market_session=market_session,
            cancel_event=cancel_event,
        )
    logger.info(f"[Main] Universe returned {len(candidates)} stage-2 candidates")
    if stage_callback:
        stage_callback(stats)

    if not candidates:
        logger.error("[Main] Universe discovery returned 0 candidates – check API connectivity")
        stats.top_picks = 0
        return [], regime, stats

    # ── 3. Deep analysis ──
    max_stage3 = CONFIG.MAX_STAGE3_PASS_EXTENDED if market_session in ("pre-market", "after-hours") else CONFIG.MAX_STAGE3_PASS
    stats.stage3_total = len(candidates[:max_stage3])
    stats.stage3_symbols = [c.symbol for c in candidates[:max_stage3]]
    if stage_callback:
        stage_callback(stats)

    with Timer("Deep Analysis"):
        results = run_deep_analysis(
            candidates[:max_stage3],
            regime,
            cancel_event=cancel_event,
            market_session=market_session,
            result_callback=result_callback,
            progress_callback=progress_callback,
        )

    top_picks = CONFIG.TOP_PICKS_EXTENDED if market_session in ("pre-market", "after-hours") else CONFIG.TOP_PICKS
    top = results[:top_picks]

    # ── 4. Persist ──
    scan_duration = time.time() - scan_start
    scan_id = DB.insert_scan(
        regime        = regime.classification,
        universe_size = len(candidates),
        candidates    = min(len(candidates), CONFIG.MAX_STAGE3_PASS),
        top_picks     = len(top),
        duration_sec  = round(scan_duration, 2),
    )
    for r in top:
        DB.insert_signal(scan_id, r.plan, r.signal, r.price)

    # ── 5. Alerts ──
    for r in top[:5]:    # alert on top 5 only
        if r.signal.composite >= CONFIG.MIN_SCORE_TO_ALERT:
            send_alert(r.plan, r.signal, r.price)

    stats.top_picks = len(top)
    logger.info(f"[Main] Scan complete in {scan_duration:.1f}s  |  "
                f"{len(top)} top picks  |  scan_id={scan_id}")

    return top, regime, stats


# ── Continuous loop (called from main.py) ────────────────────────────────────

def continuous_scan(interval_min: int = None) -> None:
    interval = (interval_min or CONFIG.SCAN_INTERVAL_MIN) * 60
    run_count = 0
    while True:
        try:
            run_count += 1
            logger.info(f"\n[Loop] === Scan #{run_count} ===\n")
            results, regime, _stats = run_full_scan()
            _print_table(results, regime)
        except KeyboardInterrupt:
            logger.info("[Loop] Interrupted by user")
            break
        except Exception as e:
            logger.error(f"[Loop] Scan error: {e}", exc_info=True)

        logger.info(f"[Loop] Next scan in {interval//60} min …")
        time.sleep(interval)


# ── Console display ───────────────────────────────────────────────────────────

def _print_table(results: List[ScanResult], regime: MarketRegime) -> None:
    from rich.table import Table
    from rich.console import Console
    from rich import box

    console = Console()
    console.print(f"\n[bold cyan]Market Regime:[/bold cyan] [bold]{regime.classification}[/bold]  "
                  f"VIX={regime.vix:.1f}  SPY vs 50SMA={regime.spy_vs_50sma:+.1f}%  "
                  f"Top sector: {regime.top_sector}\n")

    tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan",
                title=f"Top {len(results)} Setups", border_style="dim")

    tbl.add_column("#",        style="dim", width=3)
    tbl.add_column("Symbol",   style="bold white", width=7)
    tbl.add_column("Dir",      width=6)
    tbl.add_column("Score",    width=7)
    tbl.add_column("Conf",     width=10)
    tbl.add_column("Price",    width=9)
    tbl.add_column("Entry",    width=9)
    tbl.add_column("Stop",     width=9)
    tbl.add_column("TP1",      width=9)
    tbl.add_column("TP2",      width=9)
    tbl.add_column("R/R",      width=5)
    tbl.add_column("RVol",     width=6)
    tbl.add_column("Shares",   width=7)
    tbl.add_column("Risk$",    width=7)
    tbl.add_column("Flags",    width=30)

    CONF_COLORS = {"VERY HIGH": "green1", "HIGH": "chartreuse3",
                   "MEDIUM": "yellow", "LOW": "dim"}
    DIR_COLORS  = {"LONG": "green", "SHORT": "red"}

    for i, r in enumerate(results, 1):
        sig    = r.signal
        plan   = r.plan
        dc     = DIR_COLORS.get(sig.direction, "white")
        cc     = CONF_COLORS.get(sig.confidence, "white")
        change_color = "green" if r.stock.change_pct >= 0 else "red"

        tbl.add_row(
            str(i),
            r.stock.symbol,
            f"[{dc}]{sig.direction}[/{dc}]",
            f"[bold]{sig.composite:.0f}[/bold]",
            f"[{cc}]{sig.confidence}[/{cc}]",
            f"[{change_color}]{fmt_price(r.price)}[/{change_color}]",
            fmt_price(plan.entry),
            f"[red]{fmt_price(plan.stop_loss)}[/red]",
            f"[yellow]{fmt_price(plan.tp1)}[/yellow]",
            f"[green]{fmt_price(plan.tp2)}[/green]",
            f"{plan.rr_ratio:.1f}×",
            f"{r.stock.rel_volume:.1f}×",
            str(plan.shares),
            f"${plan.risk_dollars:.0f}",
            ", ".join(sig.flags) or "—",
        )

    console.print(tbl)
    console.print(f"[dim]Generated at {time.strftime('%H:%M:%S')}[/dim]\n")
