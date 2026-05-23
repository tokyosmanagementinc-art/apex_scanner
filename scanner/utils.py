"""
utils.py
Shared utilities: structured logging, file-based cache, retry decorator,
timing helpers, and formatting.
"""
import os
import json
import time
import logging
import hashlib
import functools
import traceback
from datetime import datetime, date
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from rich.console import Console
from rich.logging import RichHandler


# ── Logging setup ──────────────────────────────────────────────────────────────

def setup_logging(log_dir: str = "logs", level: str = "INFO") -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file = Path(log_dir) / f"scanner_{date.today()}.log"

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(name)-18s | %(levelname)-8s | %(message)s",
        handlers=[
            RichHandler(rich_tracebacks=True, show_path=False),
            logging.FileHandler(log_file),
        ],
        datefmt="%H:%M:%S",
    )
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    return logging.getLogger("scanner")


logger = setup_logging()
console = Console()


# ── File-based Cache ───────────────────────────────────────────────────────────

class FileCache:
    """Simple JSON key-value cache with per-entry TTL."""

    def __init__(self, cache_dir: str = "cache"):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        hashed = hashlib.md5(key.encode()).hexdigest()
        return self.dir / f"{hashed}.json"

    def _normalize(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {self._normalize_key(k): self._normalize(v)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self._normalize(v) for v in value]
        if isinstance(value, tuple):
            return [self._normalize(v) for v in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, np.generic):
            return value.item()
        return value

    def _normalize_key(self, key: Any) -> str:
        if isinstance(key, (str, int, float, bool)) or key is None:
            return str(key)
        return str(key)

    def get(self, key: str) -> Optional[Any]:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            with open(p) as f:
                entry = json.load(f)
            if time.time() > entry["expires"]:
                p.unlink(missing_ok=True)
                return None
            return entry["data"]
        except Exception:
            return None

    def set(self, key: str, value: Any, ttl: int = 300) -> None:
        p = self._path(key)
        tmp = p.with_suffix(p.suffix + ".tmp")
        try:
            normalized = self._normalize(value)
            with open(tmp, "w") as f:
                json.dump({"expires": time.time() + ttl, "data": normalized}, f)
            tmp.replace(p)
        except Exception as e:
            logger.warning(f"Cache write failed: {e}")
            tmp.unlink(missing_ok=True)

    def invalidate(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def clear_expired(self) -> int:
        removed = 0
        for p in self.dir.glob("*.json"):
            try:
                with open(p) as f:
                    entry = json.load(f)
                if time.time() > entry["expires"]:
                    p.unlink()
                    removed += 1
            except Exception:
                p.unlink(missing_ok=True)
                removed += 1
        return removed


CACHE = FileCache()


# ── Retry decorator ────────────────────────────────────────────────────────────

def retry(max_tries: int = 3, delay: float = 1.5, backoff: float = 2.0,
          exceptions: tuple = (Exception,)):
    """Exponential-backoff retry with jitter."""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            wait = delay
            for attempt in range(1, max_tries + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_tries:
                        logger.error(f"{fn.__name__} failed after {max_tries} tries: {e}")
                        raise
                    logger.warning(f"{fn.__name__} attempt {attempt} failed: {e}. Retry in {wait:.1f}s")
                    time.sleep(wait)
                    wait *= backoff
        return wrapper
    return decorator


# ── Timing helper ──────────────────────────────────────────────────────────────

class Timer:
    def __init__(self, label: str = ""):
        self.label = label

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_):
        self.elapsed = time.perf_counter() - self._start
        if self.label:
            logger.debug(f"[TIMER] {self.label}: {self.elapsed:.2f}s")


# ── Formatting ─────────────────────────────────────────────────────────────────

def fmt_pct(v: float, sign: bool = True) -> str:
    prefix = "+" if sign and v >= 0 else ""
    return f"{prefix}{v:.2f}%"

def fmt_vol(v: int) -> str:
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v/1_000:.0f}K"
    return str(v)

def fmt_price(v: float) -> str:
    return f"${v:,.2f}" if v >= 10 else f"${v:.4f}"

def fmt_cap(v: float) -> str:
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    if v >= 1e6:  return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def is_market_open() -> bool:
    """Rough US market-hours check (ET), doesn't handle holidays."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        import datetime as dt
        now = dt.datetime.utcnow()
        et_offset = -4  # EDT approximation
        now = now + dt.timedelta(hours=et_offset)

    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now < close_t


def get_market_session() -> str:
    """Return the current session used by the background scanner."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        import datetime as dt
        now = datetime.utcnow() + dt.timedelta(hours=-4)

    if now.weekday() >= 5:
        return "regular"

    minutes = now.hour * 60 + now.minute
    pre_open = 4 * 60
    regular_open = 9 * 60 + 30
    regular_close = 16 * 60
    after_close = 20 * 60

    if pre_open <= minutes < regular_open:
        return "pre-market"
    if regular_open <= minutes < regular_close:
        return "regular"
    if regular_close <= minutes < after_close:
        return "after-hours"
    return "regular"


def chunk_list(lst: list, size: int) -> list:
    return [lst[i:i+size] for i in range(0, len(lst), size)]

def safe_divide(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b != 0 else default
