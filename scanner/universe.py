"""
universe.py  –  Dynamic Universe Discovery
============================================
Zero preset watchlists.  Every scan starts fresh by:

  1. Pulling ALL listed tickers from NASDAQ's public FTP (nasdaqtrader.com).
  2. Augmenting with Yahoo Finance pre-screened movers (day_gainers, most_actives …).
  3. Applying Stage-1 cheap price/volume/market-cap filters via batch yfinance download.
  4. Returning a ranked list of candidates for the deep analysis pipeline.

Pipeline:
  ~6 000 exchange tickers
       ↓  symbol hygiene (warrants, ETNs, preferreds out)
  ~4 000 equity-only symbols
       ↓  Stage 1: batch quote filter  (price, volume, market cap)
    ~400 candidates
       ↓  Stage 2: activity filter     (relative volume ≥ threshold, gap %, price action)
    ~150 active candidates
       ↓  returned for deep analysis
"""

import re
import time
import logging
import threading
import requests
import pandas as pd
import yfinance as yf
from io import StringIO
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

from scanner.config import CONFIG
from scanner.utils import CACHE, retry, chunk_list, fmt_vol, logger

# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class UniverseStock:
    symbol:      str
    name:        str       = ""
    exchange:    str       = ""
    price:       float     = 0.0
    change_pct:  float     = 0.0
    volume:      int       = 0
    avg_volume:  int       = 0
    rel_volume:  float     = 0.0
    market_cap:  float     = 0.0
    gap_pct:     float     = 0.0          # overnight gap
    day_high:    float     = 0.0
    day_low:     float     = 0.0
    prev_close:  float     = 0.0
    float_shares: float    = 0.0
    short_pct:   float     = 0.0
    sector:      str       = ""
    industry:    str       = ""
    stage1_pass: bool      = False
    stage2_pass: bool      = False


# ── NASD FTP: full exchange listing (free, no API key) ─────────────────────────

_BAD_CHARS = re.compile(r"[\^+~$\-]")           # warrants, rights, preferreds
_DIGITS    = re.compile(r"\d{4,}")               # long all-numeric = usually invalid

def _is_valid_symbol(sym: str) -> bool:
    if not sym or len(sym) > 5:
        return False
    if _BAD_CHARS.search(sym):
        return False
    if _DIGITS.search(sym):
        return False
    return True


def _is_yf_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "too many requests" in msg
        or "rate limit" in msg
        or "yfratelimiterror" in type(exc).__name__.lower()
        or "429" in msg
    )


def _scan_cancelled(cancel_event: Optional[threading.Event]) -> bool:
    return cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)()


@retry(max_tries=3, delay=2.0)
def _fetch_nasdaq_list(url: str, exchange: str) -> pd.DataFrame:
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    lines = r.text.strip().split("\n")
    rows = []
    for line in lines[1:]:          # skip header
        parts = line.split("|")
        if len(parts) < 2:
            continue
        sym = parts[0].strip()
        if not _is_valid_symbol(sym):
            continue
        name = parts[1].strip()
        if "File Creation Time" in name or not name:
            continue
        rows.append({"symbol": sym, "name": name, "exchange": exchange})
    return pd.DataFrame(rows)


def get_full_exchange_list(force_refresh: bool = False) -> pd.DataFrame:
    """
    Pull ALL publicly listed US equities from NASDAQ Trader.
    Cached locally for UNIVERSE_CACHE_TTL seconds.
    Returns DataFrame with columns: symbol, name, exchange.
    """
    cache_key = "full_exchange_list"
    if not force_refresh:
        cached = CACHE.get(cache_key)
        if cached:
            df = pd.DataFrame(cached)
            logger.info(f"[Universe] Loaded {len(df)} tickers from cache")
            return df

    frames = []
    sources = [
        (CONFIG.NASDAQ_LISTED_URL, "NASDAQ"),
        (CONFIG.OTHER_LISTED_URL,  "NYSE/AMEX"),
    ]
    for url, exchange in sources:
        try:
            df = _fetch_nasdaq_list(url, exchange)
            frames.append(df)
            logger.info(f"[Universe] {exchange}: {len(df)} symbols")
        except Exception as e:
            logger.warning(f"[Universe] Could not fetch {exchange} list: {e}")

    if not frames:
        logger.error("[Universe] All exchange list fetches failed!")
        return pd.DataFrame(columns=["symbol", "name", "exchange"])

    result = pd.concat(frames, ignore_index=True).drop_duplicates("symbol")
    # remove common test/defunct patterns
    result = result[result["symbol"].str.len().between(1, 5)]
    logger.info(f"[Universe] Total symbols after cleanup: {len(result)}")
    CACHE.set(cache_key, result.to_dict("records"), ttl=CONFIG.UNIVERSE_CACHE_TTL)
    return result


# ── Yahoo Finance screeners (free, no key) ─────────────────────────────────────

def _yf_screener(screener_id: str, count: int = 100) -> List[str]:
    """Hit Yahoo Finance predefined screener endpoint."""
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    params = {"scrIds": screener_id, "count": count, "formatted": "false",
              "lang": "en-US", "region": "US"}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        quotes = (data.get("finance", {})
                      .get("result", [{}])[0]
                      .get("quotes", []))
        return [q["symbol"] for q in quotes if q.get("symbol")]
    except Exception as e:
        logger.debug(f"[Universe] Screener {screener_id} failed: {e}")
        return []


def get_screener_symbols(screeners: Optional[List[str]] = None) -> List[str]:
    """Collect all symbols from Yahoo Finance screeners (no key needed)."""
    if screeners is None:
        screeners = CONFIG.YF_SCREENERS

    all_syms: set = set()
    for s_id in screeners:
        syms = _yf_screener(s_id, count=100)
        all_syms.update(syms)
        logger.debug(f"[Universe] Screener {s_id}: {len(syms)} symbols")
        time.sleep(0.25)   # gentle pacing

    logger.info(f"[Universe] Yahoo screeners: {len(all_syms)} unique symbols")
    return list(all_syms)


def _download_batch_quotes_yahoo_api(symbols: List[str], market_session: str = "regular") -> Tuple[Dict[str, dict], List[str]]:
    if not symbols:
        return {}, []

    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    results: Dict[str, dict] = {}
    failed: List[str] = []

    for batch in chunk_list(symbols, 50):
        params = {"symbols": ",".join(batch)}
        r = requests.get(url, params=params, headers=headers, timeout=15)
        if r.status_code != 200:
            raise Exception(f"Yahoo quote API HTTP {r.status_code}")

        data = r.json()
        quotes = (data.get("quoteResponse", {})
                      .get("result", []))
        returned = set()

        for q in quotes:
            sym = q.get("symbol")
            if not sym:
                continue
            returned.add(sym)

            price = q.get("regularMarketPrice")
            volume = q.get("regularMarketVolume")
            change_pct = q.get("regularMarketChangePercent")
            if market_session == "pre-market" and q.get("preMarketPrice") is not None:
                price = q.get("preMarketPrice")
                volume = q.get("preMarketVolume") or volume
                change_pct = q.get("preMarketChangePercent") or change_pct
            elif market_session == "after-hours" and q.get("postMarketPrice") is not None:
                price = q.get("postMarketPrice")
                volume = q.get("postMarketVolume") or volume
                change_pct = q.get("postMarketChangePercent") or change_pct

            prev_close = q.get("regularMarketPreviousClose")
            avg_vol = q.get("averageDailyVolume3Month") or q.get("averageDailyVolume10Day") or 0
            if price is None or prev_close is None or volume is None:
                failed.append(sym)
                continue
            try:
                price = float(price)
                prev_close = float(prev_close)
                volume = int(volume)
                avg_vol = int(avg_vol) if avg_vol else volume
            except Exception:
                failed.append(sym)
                continue

            gap_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
            if change_pct is None:
                change_pct = round(gap_pct, 2)
            else:
                try:
                    change_pct = float(change_pct)
                except Exception:
                    change_pct = round(gap_pct, 2)

            results[sym] = {
                "price": price,
                "volume": volume,
                "avg_volume": avg_vol,
                "prev_close": prev_close,
                "gap_pct": round(gap_pct, 2),
                "day_high": float(q.get("regularMarketDayHigh") or 0.0),
                "day_low": float(q.get("regularMarketDayLow") or 0.0),
                "change_pct": round(change_pct, 2),
            }

        for sym in batch:
            if sym not in returned:
                failed.append(sym)

        time.sleep(0.15)

    return results, list(dict.fromkeys(failed))


def _parse_finviz_number(value: Optional[str]) -> float:
    if not value:
        return 0.0

    text = value.strip().replace(",", "").replace("%", "")
    multiplier = 1.0
    if text.endswith("B"):
        multiplier = 1e9
        text = text[:-1]
    elif text.endswith("M"):
        multiplier = 1e6
        text = text[:-1]
    elif text.endswith("K"):
        multiplier = 1e3
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return 0.0


def _finviz_snapshot_fields(html: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    row_pattern = re.compile(
        r'(<td[^>]*class="snapshot-td2[^"]*"[^>]*>.*?</td>)\s*'
        r'(<td[^>]*class="snapshot-td2[^"]*"[^>]*>.*?</td>)',
        re.S,
    )
    for label_td, value_td in row_pattern.findall(html):
        label_match = re.search(r'<div[^>]*class="snapshot-td-label"[^>]*>\s*([^<]+?)\s*</div>', label_td, re.S)
        value_match = re.search(r'<div[^>]*class="snapshot-td-content[^"]*"[^>]*>(.*?)</div>', value_td, re.S)
        if not label_match or not value_match:
            continue
        label = label_match.group(1).strip()
        value = re.sub(r'<[^>]+>', '', value_match.group(1)).strip()
        fields[label] = value
    return fields


def _finviz_js_field(html: str, key: str) -> Optional[float]:
    match = re.search(rf'"{re.escape(key)}":([0-9\.eE+-]+)', html)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _download_batch_quotes_finviz(symbols: List[str]) -> Tuple[Dict[str, dict], List[str]]:
    if not symbols:
        return {}, []

    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
    results: Dict[str, dict] = {}
    failed: List[str] = []

    for sym in symbols:
        try:
            r = requests.get("https://finviz.com/quote.ashx", params={"t": sym}, headers=headers, timeout=15)
            if r.status_code != 200:
                raise Exception(f"Finviz HTTP {r.status_code}")
            html = r.text
            fields = _finviz_snapshot_fields(html)

            price = _finviz_js_field(html, "lastClose") or _parse_finviz_number(fields.get("Price"))
            prev_close = _finviz_js_field(html, "prevClose") or _parse_finviz_number(fields.get("Prev Close"))
            volume = int(_finviz_js_field(html, "lastVolume") or _parse_finviz_number(fields.get("Volume")) or 0)
            avg_vol = int(_parse_finviz_number(fields.get("Avg Volume")) or volume)
            day_high = _finviz_js_field(html, "lastHigh") or _parse_finviz_number(fields.get("Day High"))
            day_low = _finviz_js_field(html, "lastLow") or _parse_finviz_number(fields.get("Day Low"))
            change_pct = _finviz_js_field(html, "perfDayPct")
            if change_pct is None:
                change_pct = _parse_finviz_number(fields.get("Change", "0"))

            if not price or prev_close is None or not volume:
                raise ValueError("Incomplete Finviz quote")

            gap_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
            results[sym] = {
                "price": float(price),
                "volume": volume,
                "avg_volume": avg_vol,
                "prev_close": float(prev_close),
                "gap_pct": round(gap_pct, 2),
                "day_high": float(day_high or 0.0),
                "day_low": float(day_low or 0.0),
                "change_pct": round(change_pct or gap_pct, 2),
            }
        except Exception as e:
            logger.debug(f"[Universe] Finviz quote failed for {sym}: {e}")
            failed.append(sym)
        finally:
            time.sleep(0.15)

    return results, list(dict.fromkeys(failed))


def _download_batch_quotes_finnhub(symbols: List[str]) -> Tuple[Dict[str, dict], List[str]]:
    if not symbols or not CONFIG.FINNHUB_KEY:
        return {}, symbols

    url = "https://finnhub.io/api/v1/quote"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    results: Dict[str, dict] = {}
    failed: List[str] = []

    for sym in symbols:
        params = {"symbol": sym, "token": CONFIG.FINNHUB_KEY}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            q = r.json()
            price = q.get("c")
            prev_close = q.get("pc")
            volume = q.get("v")
            if price is None or prev_close is None or volume is None:
                failed.append(sym)
                continue
            price = float(price)
            prev_close = float(prev_close)
            volume = int(volume)
            avg_vol = volume
            gap_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
            results[sym] = {
                "price": price,
                "volume": volume,
                "avg_volume": avg_vol,
                "prev_close": prev_close,
                "gap_pct": round(gap_pct, 2),
                "day_high": price,
                "day_low": price,
                "change_pct": round(q.get("dp", gap_pct) or gap_pct, 2),
            }
        except Exception as e:
            logger.debug(f"[Universe] Finnhub quote failed for {sym}: {e}")
            failed.append(sym)
        finally:
            time.sleep(0.15)

    return results, failed


def _download_batch_quotes_alpha_vantage(symbols: List[str]) -> Tuple[Dict[str, dict], List[str]]:
    if not symbols or not CONFIG.ALPHA_VANTAGE_KEY:
        return {}, symbols

    url = "https://www.alphavantage.co/query"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    results: Dict[str, dict] = {}
    failed: List[str] = []

    for sym in symbols:
        params = {
            "function": "GLOBAL_QUOTE",
            "symbol": sym,
            "apikey": CONFIG.ALPHA_VANTAGE_KEY,
        }
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            data = r.json().get("Global Quote", {})
            price = data.get("05. price")
            prev_close = data.get("08. previous close")
            volume = data.get("06. volume")
            if price is None or prev_close is None or volume is None:
                failed.append(sym)
                continue
            price = float(price)
            prev_close = float(prev_close)
            volume = int(volume)
            avg_vol = volume
            gap_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
            change_pct = float(data.get("10. change percent", gap_pct).replace("%", "")) if data.get("10. change percent") else gap_pct
            results[sym] = {
                "price": price,
                "volume": volume,
                "avg_volume": avg_vol,
                "prev_close": prev_close,
                "gap_pct": round(gap_pct, 2),
                "day_high": price,
                "day_low": price,
                "change_pct": round(change_pct, 2),
            }
        except Exception as e:
            logger.debug(f"[Universe] AlphaVantage quote failed for {sym}: {e}")
            failed.append(sym)
        finally:
            time.sleep(12.0 / max(len(symbols), 1))

    return results, failed


# ── Stage 1: Cheap batch filter ────────────────────────────────────────────────

@retry(max_tries=3, delay=2.5, backoff=2.0, exceptions=(Exception,))
def _download_batch_quotes(symbols: List[str], market_session: str = "regular") -> Tuple[Dict[str, dict], List[str]]:
    """
    Download the most-recent 5 days of OHLCV for a batch of symbols.
    Returns (results, failed_symbols).
    Uses yfinance batch download (single HTTP request for whole batch).
    Supports session-specific quote lookups for pre-market and after-hours scans.
    """
    if not symbols:
        return {}, []

    try:
        results, failed = _download_batch_quotes_yahoo_api(symbols, market_session=market_session)
        if failed:
            finviz_results, failed = _download_batch_quotes_finviz(failed)
            results.update(finviz_results)
        if failed and results:
            logger.info(f"[Universe] Yahoo batch returned {len(results)} quotes, trying fallback for {len(failed)} symbols")
            secondary, failed = _download_batch_quotes_finnhub(failed)
            results.update(secondary)
        if failed and CONFIG.ALPHA_VANTAGE_KEY:
            tertiary, failed = _download_batch_quotes_alpha_vantage(failed)
            results.update(tertiary)
        if results or not failed:
            return results, failed
    except Exception as e:
        logger.warning(f"[Universe] Yahoo quote API failed: {e}. Attempting alternative quote providers.")

    if results:
        return results, failed

    results, failed = _download_batch_quotes_finviz(symbols)
    if results or not failed:
        return results, failed
    if CONFIG.FINNHUB_KEY:
        results, failed = _download_batch_quotes_finnhub(symbols)
        if results or not failed:
            return results, failed
    if CONFIG.ALPHA_VANTAGE_KEY:
        results, failed = _download_batch_quotes_alpha_vantage(symbols)
        if results or not failed:
            return results, failed

    try:
        raw = yf.download(
            tickers=symbols,
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=False,
        )
    except Exception as e:
        if _is_yf_rate_limit_error(e):
            logger.warning(f"[Universe] yfinance rate-limited on batch download: {e}")
            raise
        logger.warning(f"[Universe] Batch download error: {e}")
        return {}, symbols

    results = {}
    failed: List[str] = []
    multi = isinstance(raw.columns, pd.MultiIndex)

    for sym in symbols:
        try:
            df = raw[sym] if multi else raw
            if df is None or df.empty or len(df) < 2:
                failed.append(sym)
                continue
            df = df.dropna()
            if len(df) < 2:
                failed.append(sym)
                continue

            today  = df.iloc[-1]
            prev   = df.iloc[-2]

            price      = float(today["Close"])
            volume     = int(today["Volume"])
            prev_close = float(prev["Close"])
            gap_pct    = ((price - prev_close) / prev_close * 100) if prev_close else 0
            avg_vol    = int(df["Volume"].mean())

            results[sym] = {
                "price":      price,
                "volume":     volume,
                "avg_volume": avg_vol,
                "prev_close": prev_close,
                "gap_pct":    round(gap_pct, 2),
                "day_high":   float(today["High"]),
                "day_low":    float(today["Low"]),
                "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
            }
        except Exception:
            failed.append(sym)

    return results, failed


def stage1_filter(symbols: List[str], market_session: str = "regular", cfg=CONFIG, cancel_event: Optional[threading.Event] = None) -> List[UniverseStock]:
    """
    Apply cheap filters: price, volume, rough market-cap proxy.
    Uses smaller yfinance batch downloads with retry loops to avoid rate limits.
    Supports session-specific quote selection for regular, pre-market, and after-hours.
    Returns a list of UniverseStock objects that pass.
    """
    logger.info(f"[Stage1] Filtering {len(symbols)} symbols for session={market_session} …")
    remaining = symbols[:]
    all_quotes: Dict[str, dict] = {}
    attempt = 0
    if market_session in ("pre-market", "after-hours"):
        batch_size = max(1, min(cfg.YF_BATCH_SIZE_STAGE1_EXTENDED, len(symbols)))
        max_quotes = cfg.MAX_STAGE1_PASS_EXTENDED
    else:
        batch_size = max(1, min(cfg.YF_BATCH_SIZE_STAGE1, len(symbols)))
        max_quotes = cfg.MAX_STAGE1_PASS

    while remaining and attempt <= cfg.YF_BATCH_RETRIES and len(all_quotes) < max_quotes:
        attempt += 1
        if attempt > 1:
            logger.info(f"[Stage1] Retry round {attempt}/{cfg.YF_BATCH_RETRIES + 1} for {len(remaining)} failed symbols")
            time.sleep(cfg.YF_BATCH_RETRY_DELAY * attempt)
            if batch_size > 1:
                batch_size = max(1, batch_size // 2)
                logger.info(f"[Stage1] Reducing batch size to {batch_size} for retry")

        batches = chunk_list(remaining, batch_size)
        max_workers = min(2, max(1, len(batches)))
        next_round: List[str] = []

        try:
            if _scan_cancelled(cancel_event):
                logger.info("[Stage1] Cancel requested, aborting stage 1")
                raise Exception("Scan canceled")
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_download_batch_quotes, b, market_session): b for b in batches}
                for i, fut in enumerate(as_completed(futures)):
                    if _scan_cancelled(cancel_event):
                        logger.info("[Stage1] Cancel requested, stopping stage 1 batch processing")
                        raise Exception("Scan canceled")
                    quotes, failed = fut.result()
                    all_quotes.update(quotes)
                    next_round.extend(failed)
                    if i % 5 == 0:
                        logger.debug(f"[Stage1] Batches done: {i+1}/{len(batches)}, "
                                     f"quotes so far: {len(all_quotes)}")
        except RuntimeError as e:
            logger.warning(f"[Universe] Stage1 thread pool creation failed: {e}. Falling back to serial batch download.")
            next_round = []
            for i, b in enumerate(batches):
                quotes, failed = _download_batch_quotes(b, market_session)
                all_quotes.update(quotes)
                next_round.extend(failed)
                if i % 5 == 0:
                    logger.debug(f"[Stage1] Batches done: {i+1}/{len(batches)}, "
                                 f"quotes so far: {len(all_quotes)}")

        remaining = list(dict.fromkeys(next_round))
        if len(all_quotes) >= max_quotes:
            break

    if remaining:
        logger.debug(f"[Stage1] {len(remaining)} symbols could not be downloaded after retries")

    passing: List[UniverseStock] = []
    for sym, q in all_quotes.items():
        price   = q["price"]
        volume  = q["volume"]
        avg_vol = q.get("avg_volume", 0)

        # price range
        if not (cfg.MIN_PRICE <= price <= cfg.MAX_PRICE):
            continue
        # minimum absolute volume
        min_vol = cfg.MIN_VOLUME_EXTENDED if market_session in ("pre-market", "after-hours") else cfg.MIN_VOLUME
        if volume < min_vol:
            continue
        # very rough market cap proxy: price × avg_vol × ~10 trading days
        mkt_cap_proxy = price * avg_vol * 10
        if mkt_cap_proxy < (cfg.MIN_MARKET_CAP_EXTENDED if market_session in ("pre-market", "after-hours") else cfg.MIN_MARKET_CAP):
            continue

        passing.append(UniverseStock(
            symbol      = sym,
            price       = price,
            volume      = volume,
            avg_volume  = avg_vol,
            prev_close  = q["prev_close"],
            gap_pct     = q["gap_pct"],
            change_pct  = q["change_pct"],
            day_high    = q["day_high"],
            day_low     = q["day_low"],
            stage1_pass = True,
        ))

    logger.info(f"[Stage1] {len(passing)} / {len(symbols)} passed")
    return passing


# ── Stage 2: Activity filter ────────────────────────────────────────────────────

def _download_quote_info_batch(symbols: List[str], cfg=CONFIG, cancel_event: Optional[threading.Event] = None) -> Dict[str, dict]:
    if not symbols:
        return {}

    if _scan_cancelled(cancel_event):
        return {}

    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    info_map: Dict[str, dict] = {}

    with requests.Session() as session:
        for batch in chunk_list(symbols, 50):
            if _scan_cancelled(cancel_event):
                return info_map
            params = {"symbols": ",".join(batch)}
            r = session.get(url, params=params, headers=headers, timeout=15)
        if r.status_code != 200:
            raise Exception(f"Yahoo quote API HTTP {r.status_code}")

        data = r.json()
        quotes = (data.get("quoteResponse", {})
                      .get("result", []))
        for q in quotes:
            sym = q.get("symbol")
            if not sym:
                continue
            market_cap = q.get("marketCap") or 0.0
            float_shares = q.get("sharesOutstanding") or 0.0
            info_map[sym] = {
                "market_cap": float(market_cap) if market_cap else 0.0,
                "float_shares": float(float_shares) if float_shares else 0.0,
            }
        time.sleep(cfg.YF_INFO_DELAY)

    return info_map


def _download_quote_info_batch_finnhub(symbols: List[str]) -> Dict[str, dict]:
    info_map: Dict[str, dict] = {}
    if not symbols or not CONFIG.FINNHUB_KEY:
        return info_map

    url = "https://finnhub.io/api/v1/stock/profile2"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for sym in symbols:
        params = {"symbol": sym, "token": CONFIG.FINNHUB_KEY}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
            market_cap = data.get("marketCapitalization") or 0.0
            float_shares = data.get("shareOutstanding") or 0.0
            if market_cap and float_shares:
                info_map[sym] = {
                    "market_cap": float(market_cap),
                    "float_shares": float(float_shares),
                }
        except Exception as e:
            logger.debug(f"[Universe] Finnhub profile failed for {sym}: {e}")
        finally:
            time.sleep(0.15)
    return info_map


def _download_quote_info_batch_alpha_vantage(symbols: List[str]) -> Dict[str, dict]:
    info_map: Dict[str, dict] = {}
    if not symbols or not CONFIG.ALPHA_VANTAGE_KEY:
        return info_map

    url = "https://www.alphavantage.co/query"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for sym in symbols:
        params = {
            "function": "OVERVIEW",
            "symbol": sym,
            "apikey": CONFIG.ALPHA_VANTAGE_KEY,
        }
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            data = r.json()
            market_cap = data.get("MarketCapitalization")
            float_shares = data.get("SharesOutstanding")
            if market_cap and float_shares:
                info_map[sym] = {
                    "market_cap": float(market_cap),
                    "float_shares": float(float_shares),
                }
        except Exception as e:
            logger.debug(f"[Universe] AlphaVantage overview failed for {sym}: {e}")
        finally:
            time.sleep(12.0 / max(len(symbols), 1))
    return info_map


def _download_quote_info_batch_finviz(symbols: List[str]) -> Dict[str, dict]:
    info_map: Dict[str, dict] = {}
    if not symbols:
        return info_map

    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
    for sym in symbols:
        try:
            r = requests.get("https://finviz.com/quote.ashx", params={"t": sym}, headers=headers, timeout=15)
            if r.status_code != 200:
                raise Exception(f"Finviz HTTP {r.status_code}")
            html = r.text
            fields = _finviz_snapshot_fields(html)
            market_cap = _parse_finviz_number(fields.get("Market Cap"))
            float_shares = _parse_finviz_number(fields.get("Shs Float") or fields.get("Shs Outstand"))
            if market_cap and float_shares:
                info_map[sym] = {
                    "market_cap": market_cap,
                    "float_shares": float_shares,
                }
        except Exception as e:
            logger.debug(f"[Universe] Finviz profile failed for {sym}: {e}")
        finally:
            time.sleep(0.15)
    return info_map


def _enrich_with_info(stocks: List[UniverseStock], cfg=CONFIG, cancel_event: Optional[threading.Event] = None) -> None:
    """
    Enrich passing symbols with market cap, float, and basic quote metadata.
    Uses Yahoo quote API in batches and falls back to other providers.
    """
    if not stocks:
        return

    for batch in chunk_list([s.symbol for s in stocks], cfg.YF_INFO_WORKERS):
        if _scan_cancelled(cancel_event):
            logger.info("[Stage2] Cancel requested, aborting info enrichment")
            raise Exception("Scan canceled")
        info_map: Dict[str, dict] = {}
        try:
            info_map = _download_quote_info_batch(batch, cfg, cancel_event)
        except Exception as e:
            logger.warning(f"[Universe] Yahoo quote info batch failed: {e}")

        if len(info_map) < len(batch):
            if _scan_cancelled(cancel_event):
                raise Exception("Scan canceled")
            try:
                fallback = _download_quote_info_batch_finviz(batch)
                info_map.update(fallback)
            except Exception as e:
                logger.warning(f"[Universe] Finviz quote info batch failed: {e}")

        if len(info_map) < len(batch):
            if _scan_cancelled(cancel_event):
                raise Exception("Scan canceled")
            try:
                fallback = _download_quote_info_batch_finnhub(batch)
                info_map.update(fallback)
            except Exception as e:
                logger.warning(f"[Universe] Finnhub quote info batch failed: {e}")

        if len(info_map) < len(batch) and CONFIG.ALPHA_VANTAGE_KEY:
            if _scan_cancelled(cancel_event):
                raise Exception("Scan canceled")
            try:
                fallback = _download_quote_info_batch_alpha_vantage(batch)
                info_map.update(fallback)
            except Exception as e:
                logger.warning(f"[Universe] AlphaVantage quote info batch failed: {e}")

        for stock in stocks:
            if stock.symbol not in info_map:
                continue
            info = info_map[stock.symbol]
            stock.market_cap = info.get("market_cap", 0.0) or 0.0
            stock.float_shares = info.get("float_shares", 0.0) or 0.0

        for stock in stocks:
            if stock.market_cap > 0 and stock.float_shares > 0:
                continue
            try:
                info = _download_fast_info(stock.symbol)
                stock.market_cap = getattr(info, "market_cap", 0.0) or 0.0
                stock.float_shares = getattr(info, "shares_outstanding", 0.0) or 0.0
            except Exception as inner:
                if _is_yf_rate_limit_error(inner):
                    logger.warning(f"[Universe] fast_info rate-limited for {stock.symbol}: {inner}")
                else:
                    logger.debug(f"[Universe] fast_info failed for {stock.symbol}: {inner}")
            finally:
                time.sleep(cfg.YF_INFO_DELAY)

        time.sleep(cfg.YF_INFO_DELAY)


def stage2_filter(stocks: List[UniverseStock], market_session: str = "regular", cfg=CONFIG, cancel_event: Optional[threading.Event] = None) -> List[UniverseStock]:
    """
    Activity filter: relative volume, gap activity, minimum market cap.
    Cheaper than deep analysis but still cuts the list significantly.
    """
    # Pre-filter by relative volume to reduce expensive yfinance info calls.
    for s in stocks:
        s.rel_volume = s.volume / s.avg_volume if s.avg_volume else 0.0

    min_rel = cfg.MIN_REL_VOLUME_EXTENDED if market_session in ("pre-market", "after-hours") else cfg.MIN_REL_VOLUME
    max_stage2 = cfg.MAX_STAGE2_PASS_EXTENDED if market_session in ("pre-market", "after-hours") else cfg.MAX_STAGE2_PASS
    min_market_cap = cfg.MIN_MARKET_CAP_EXTENDED if market_session in ("pre-market", "after-hours") else cfg.MIN_MARKET_CAP

    active = [s for s in stocks if s.rel_volume >= min_rel]
    active.sort(key=lambda x: x.rel_volume, reverse=True)

    logger.info(f"[Stage2] {len(active)} symbols passed rel-vol pre-filter for session={market_session}")
    to_enrich = active[: max(max_stage2 * 2, max_stage2)]

    # Enrich only the most promising candidates with market cap / float.
    logger.info(f"[Stage2] Enriching {len(to_enrich)} symbols …")
    _enrich_with_info(to_enrich, cfg, cancel_event)

    passing: List[UniverseStock] = []
    for s in to_enrich:
        if s.market_cap > 0 and s.market_cap < min_market_cap:
            continue
        s.stage2_pass = True
        passing.append(s)

    passing.sort(key=lambda x: x.rel_volume, reverse=True)
    result = passing[:max_stage2]
    logger.info(f"[Stage2] {len(result)} candidates after activity filter")
    return result


# ── Public entry point ──────────────────────────────────────────────────────────

@dataclass
class UniverseStats:
    universe_size: int = 0
    screener_count: int = 0
    stage1_total: int = 0
    stage1_passed: int = 0
    stage1_symbols: List[str] = field(default_factory=list)
    stage2_passed: int = 0
    stage2_symbols: List[str] = field(default_factory=list)
    stage3_total: int = 0
    stage3_symbols: List[str] = field(default_factory=list)
    top_picks: int = 0


def discover_universe(force_refresh: bool = False, market_session: str = "regular", cancel_event: Optional[threading.Event] = None) -> Tuple[List[UniverseStock], UniverseStats]:
    """
    Full dynamic universe discovery.
    Returns Stage-2 candidates ready for deep technical analysis and universe stats.

    Flow:
      NASDAQ FTP list  ──┐
      YF Screeners     ──┼──► deduplicate ──► Stage1 ──► Stage2 ──► sorted candidates
      (no preset list)   │
    """
    stats = UniverseStats()

    # 1. Full exchange symbol list (dynamic, updated daily by NASDAQ)
    full_df = get_full_exchange_list(force_refresh=force_refresh)
    all_symbols = full_df["symbol"].tolist()

    # 2. Augment with screener hot-list (these are already active today)
    screener_syms = get_screener_symbols()
    combined = list(dict.fromkeys(screener_syms + all_symbols))  # screeners first
    combined = combined[:CONFIG.MAX_UNIVERSE_SIZE]

    stats.universe_size = len(combined)
    stats.stage1_total = len(combined)
    stats.screener_count = len(screener_syms)

    logger.info(f"[Universe] Working universe: {stats.universe_size} unique symbols "
                f"({stats.screener_count} from screeners + exchange list)")

    # 3. Stage 1 cheap filter
    stage1 = stage1_filter(combined, market_session=market_session, cfg=CONFIG, cancel_event=cancel_event)
    stats.stage1_passed = len(stage1)
    max_stage1 = CONFIG.MAX_STAGE1_PASS_EXTENDED if market_session in ("pre-market", "after-hours") else CONFIG.MAX_STAGE1_PASS
    stats.stage1_symbols = [s.symbol for s in stage1[:max_stage1]]
    stage1 = stage1[:max_stage1]

    # 4. Stage 2 activity filter
    stage2 = stage2_filter(stage1, market_session=market_session, cfg=CONFIG, cancel_event=cancel_event)
    stats.stage2_passed = len(stage2)
    stats.stage2_symbols = [s.symbol for s in stage2]

    return stage2, stats
