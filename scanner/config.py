"""
config.py
All scanner configuration, loaded from environment variables with sane defaults.
"""
import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()

def _f(key: str, default: float) -> float:
    return float(os.getenv(key, default))

def _i(key: str, default: int) -> int:
    return int(os.getenv(key, default))

def _s(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass
class ScannerConfig:
    # ── Universe Filters ──────────────────────────────────────────────────────
    MIN_PRICE:           float = field(default_factory=lambda: _f("MIN_PRICE", 0.10))
    MAX_PRICE:           float = field(default_factory=lambda: _f("MAX_PRICE", 20.0))
    MIN_VOLUME:          int   = field(default_factory=lambda: _i("MIN_VOLUME", 25_000))
    MIN_REL_VOLUME:      float = field(default_factory=lambda: _f("MIN_REL_VOLUME", 1.0))
    MIN_MARKET_CAP:      float = field(default_factory=lambda: _f("MIN_MARKET_CAP", 500_000))

    # Extended-hours thresholds
    MIN_VOLUME_EXTENDED:          int   = field(default_factory=lambda: _i("MIN_VOLUME_EXTENDED", 30_000))
    MIN_REL_VOLUME_EXTENDED:      float = field(default_factory=lambda: _f("MIN_REL_VOLUME_EXTENDED", 2.2))
    MIN_MARKET_CAP_EXTENDED:      float = field(default_factory=lambda: _f("MIN_MARKET_CAP_EXTENDED", 150_000_000))

    # ── Pipeline Limits ───────────────────────────────────────────────────────
    MAX_UNIVERSE_SIZE:   int   = field(default_factory=lambda: _i("MAX_UNIVERSE_SIZE", 6000))
    MAX_STAGE1_PASS:     int   = field(default_factory=lambda: _i("MAX_STAGE1_PASS", 500))
    MAX_STAGE2_PASS:     int   = field(default_factory=lambda: _i("MAX_STAGE2_PASS", 150))
    MAX_STAGE3_PASS:     int   = field(default_factory=lambda: _i("MAX_STAGE3_PASS", 50))
    TOP_PICKS:           int   = field(default_factory=lambda: _i("TOP_PICKS", 50))
    SCAN_INTERVAL_MIN:   int   = field(default_factory=lambda: _i("SCAN_INTERVAL_MINUTES", 15))
    YF_BATCH_SIZE_STAGE1: int  = field(default_factory=lambda: _i("YF_BATCH_SIZE_STAGE1", 10))
    YF_BATCH_RETRIES:     int  = field(default_factory=lambda: _i("YF_BATCH_RETRIES", 3))
    YF_BATCH_RETRY_DELAY: float = field(default_factory=lambda: _f("YF_BATCH_RETRY_DELAY", 3.0))
    YF_INFO_WORKERS:      int  = field(default_factory=lambda: _i("YF_INFO_WORKERS", 1))
    YF_INFO_DELAY:        float = field(default_factory=lambda: _f("YF_INFO_DELAY", 0.20))

    # Extended-hours pipeline tuning
    MAX_STAGE1_PASS_EXTENDED:   int   = field(default_factory=lambda: _i("MAX_STAGE1_PASS_EXTENDED", 300))
    MAX_STAGE2_PASS_EXTENDED:   int   = field(default_factory=lambda: _i("MAX_STAGE2_PASS_EXTENDED", 90))
    MAX_STAGE3_PASS_EXTENDED:   int   = field(default_factory=lambda: _i("MAX_STAGE3_PASS_EXTENDED", 50))
    TOP_PICKS_EXTENDED:         int   = field(default_factory=lambda: _i("TOP_PICKS_EXTENDED", 18))
    SCAN_INTERVAL_MIN_EXTENDED: int   = field(default_factory=lambda: _i("SCAN_INTERVAL_MINUTES_EXTENDED", 10))
    YF_BATCH_SIZE_STAGE1_EXTENDED: int = field(default_factory=lambda: _i("YF_BATCH_SIZE_STAGE1_EXTENDED", 8))
    YF_INFO_DELAY_EXTENDED:        float = field(default_factory=lambda: _f("YF_INFO_DELAY_EXTENDED", 0.30))
    YF_BATCH_RETRY_DELAY: float = field(default_factory=lambda: _f("YF_BATCH_RETRY_DELAY", 3.0))
    YF_INFO_WORKERS:      int  = field(default_factory=lambda: _i("YF_INFO_WORKERS", 1))
    YF_INFO_DELAY:        float = field(default_factory=lambda: _f("YF_INFO_DELAY", 0.20))

    # ── Technical Indicator Params ────────────────────────────────────────────
    EMA_SHORT:   int = 9
    EMA_MID:     int = 21
    EMA_LONG:    int = 20
    SMA_200:     int = 200
    RSI_PERIOD:  int = 14
    MACD_FAST:   int = 12
    MACD_SLOW:   int = 26
    MACD_SIGNAL: int = 9
    BB_PERIOD:   int = 20
    BB_STD:      float = 2.0
    ATR_PERIOD:  int = 14
    ADX_PERIOD:  int = 14
    STOCH_K:     int = 14
    STOCH_D:     int = 3
    RVOL_PERIOD: int = 20  # days for avg volume baseline

    # ── Scoring Weights (sum = 1.0) ────────────────────────────────────────────
    W_MOMENTUM:  float = 0.28
    W_VOLUME:    float = 0.24
    W_TECHNICAL: float = 0.20
    W_RS:        float = 0.16
    W_SECTOR:    float = 0.12

    # Extended-hours weight tuning
    W_MOMENTUM_EXTENDED: float = field(default_factory=lambda: _f("W_MOMENTUM_EXTENDED", 0.30))
    W_VOLUME_EXTENDED:   float = field(default_factory=lambda: _f("W_VOLUME_EXTENDED", 0.26))
    W_TECHNICAL_EXTENDED: float = field(default_factory=lambda: _f("W_TECHNICAL_EXTENDED", 0.18))
    W_RS_EXTENDED:       float = field(default_factory=lambda: _f("W_RS_EXTENDED", 0.14))
    W_SECTOR_EXTENDED:   float = field(default_factory=lambda: _f("W_SECTOR_EXTENDED", 0.12))

    # ── Risk Management ───────────────────────────────────────────────────────
    ACCOUNT_SIZE:            float = field(default_factory=lambda: _f("ACCOUNT_SIZE", 130.0))
    MAX_RISK_PER_TRADE_PCT:  float = field(default_factory=lambda: _f("MAX_RISK_PER_TRADE_PCT", 5.0))
    MIN_RISK_REWARD:         float = field(default_factory=lambda: _f("MIN_RISK_REWARD", 1.2))
    MAX_SECTOR_EXPOSURE_PCT: float = 0.20
    ATR_STOP_MULTIPLIER:     float = 1.0

    # ── Market Regime ─────────────────────────────────────────────────────────
    REGIME_LOOKBACK:    int   = 50
    VIX_HIGH:           float = 25.0
    VIX_EXTREME:        float = 35.0
    BREADTH_BULL_THRESH: float = 0.60  # >60% of S&P stocks above 50SMA = bull

    # ── Benchmarks & Sector ETFs ──────────────────────────────────────────────
    BENCHMARKS: List[str] = field(default_factory=lambda: ["SPY", "QQQ", "IWM"])
    SECTOR_ETFS: List[str] = field(default_factory=lambda: [
        "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLB", "XLC", "XLRE",
        "XBI", "SMH", "ARKK", "IBB", "GDX", "XOP",
    ])

    # ── API Keys (free tiers) ─────────────────────────────────────────────────
    FINNHUB_KEY:       str = field(default_factory=lambda: _s("d86a6lpr01qnvdehlt20d86a6lpr01qnvdehlt2g"))
    ALPHA_VANTAGE_KEY: str = field(default_factory=lambda: _s("UOF6332A07D5Y2CV"))

    # ── Alerts ────────────────────────────────────────────────────────────────
    DISCORD_WEBHOOK:    str = field(default_factory=lambda: _s("DISCORD_WEBHOOK"))
    TELEGRAM_TOKEN:     str = field(default_factory=lambda: _s("TELEGRAM_TOKEN"))
    TELEGRAM_CHAT_ID:   str = field(default_factory=lambda: _s("TELEGRAM_CHAT_ID"))
    MIN_SCORE_TO_ALERT: float = 65.0

    # ── Storage ───────────────────────────────────────────────────────────────
    DB_PATH:            str = field(default_factory=lambda: _s("DB_PATH", "data/scanner.db"))
    LOG_DIR:            str = "logs"
    CACHE_DIR:          str = "cache"
    CACHE_TTL_SECS:     int = 300     # 5 min price cache
    UNIVERSE_CACHE_TTL: int = 3600    # 1 hour full universe list cache

    # ── Universe Data URLs (free, NASDAQ) ─────────────────────────────────────
    NASDAQ_LISTED_URL: str = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
    OTHER_LISTED_URL:  str = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

    # ── Yahoo Finance Screener IDs (free, no key) ─────────────────────────────
    YF_SCREENERS: List[str] = field(default_factory=lambda: [
        "day_gainers",
        "day_losers",
        "small_cap_gainers",
        "aggressive_small_caps",
    ])


# Singleton
CONFIG = ScannerConfig()
