"""
backtest.py  –  Signal Backtesting Engine
===========================================
Replays historical scanner signals against real price data.

Methodology:
  • Enters at next-bar open (avoids look-ahead bias)
  • Exits at TP1, TP2, TP3 or Stop Loss (whichever hits first)
  • Accounts for simple slippage and commission
  • Reports: win rate, expectancy, profit factor, Sharpe, max drawdown
"""

import math
import numpy as np
import pandas as pd
import yfinance as yf
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from datetime import datetime

from scanner.config import CONFIG
from scanner.utils import logger, fmt_price, fmt_pct


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    symbol:       str
    direction:    str
    entry_date:   str
    exit_date:    str
    entry_price:  float
    exit_price:   float
    stop_loss:    float
    tp1:          float
    tp2:          float
    shares:       int
    pnl:          float
    pnl_pct:      float
    exit_reason:  str    # 'TP1' | 'TP2' | 'TP3' | 'STOP' | 'TIME'
    r_multiple:   float  # pnl / initial_risk


@dataclass
class BacktestReport:
    trades:          List[BacktestTrade]
    total_trades:    int  = 0
    wins:            int  = 0
    losses:          int  = 0
    win_rate:        float = 0.0
    avg_win:         float = 0.0
    avg_loss:        float = 0.0
    expectancy:      float = 0.0      # per-trade expected value
    profit_factor:   float = 0.0
    total_pnl:       float = 0.0
    max_drawdown:    float = 0.0
    sharpe_ratio:    float = 0.0
    avg_r_multiple:  float = 0.0

    def print_summary(self) -> None:
        print("\n" + "="*55)
        print("  BACKTEST RESULTS")
        print("="*55)
        print(f"  Trades        : {self.total_trades}")
        print(f"  Win Rate      : {self.win_rate*100:.1f}%  ({self.wins}W / {self.losses}L)")
        print(f"  Avg Win       : ${self.avg_win:.2f}")
        print(f"  Avg Loss      : ${self.avg_loss:.2f}")
        print(f"  Expectancy    : ${self.expectancy:.2f} per trade")
        print(f"  Profit Factor : {self.profit_factor:.2f}")
        print(f"  Total P&L     : ${self.total_pnl:.2f}")
        print(f"  Max Drawdown  : ${self.max_drawdown:.2f}")
        print(f"  Sharpe Ratio  : {self.sharpe_ratio:.2f}")
        print(f"  Avg R-Multiple: {self.avg_r_multiple:.2f}R")
        print("="*55)

        if not self.trades:
            return
        print(f"\n  {'Symbol':<8} {'Dir':<6} {'Entry':<12} {'Exit':<12} "
              f"{'P&L':>9} {'R':>6} {'Reason':<8}")
        print("  " + "-"*60)
        for t in self.trades[-20:]:   # last 20
            pnl_s = f"+${t.pnl:.2f}" if t.pnl > 0 else f"-${abs(t.pnl):.2f}"
            print(f"  {t.symbol:<8} {t.direction:<6} {t.entry_price:<12.4f} "
                  f"{t.exit_price:<12.4f} {pnl_s:>9} {t.r_multiple:>5.2f}R "
                  f"{t.exit_reason:<8}")
        print()


# ── Simulation ────────────────────────────────────────────────────────────────

@dataclass
class BacktestSignal:
    """A past signal to replay."""
    symbol:     str
    direction:  str
    entry:      float
    stop_loss:  float
    tp1:        float
    tp2:        float
    tp3:        float
    shares:     int
    signal_date: str    # YYYY-MM-DD


def _fetch_forward_bars(symbol: str, from_date: str,
                        bars: int = 10) -> Optional[pd.DataFrame]:
    """Fetch bars starting from signal_date for forward testing."""
    try:
        df = yf.download(symbol, start=from_date, period="1mo",
                         interval="1d", auto_adjust=True, progress=False)
        if df is None or df.empty:
            return None
        return df.iloc[:bars]
    except Exception:
        return None


def simulate_trade(sig: BacktestSignal,
                   slippage_pct: float = 0.001,
                   commission: float = 1.0,
                   max_hold_bars: int = 5) -> Optional[BacktestTrade]:
    """
    Simulate one trade from signal to exit.
    Entry is at next-bar open (avoids look-ahead).
    Returns BacktestTrade or None if data unavailable.
    """
    df = _fetch_forward_bars(sig.symbol, sig.signal_date, bars=max_hold_bars + 2)
    if df is None or len(df) < 2:
        return None

    # Entry: next bar's open + slippage
    entry_bar   = df.iloc[1]
    entry_date  = str(df.index[1].date())
    if sig.direction == "LONG":
        entry_price = float(entry_bar["Open"]) * (1 + slippage_pct)
    else:
        entry_price = float(entry_bar["Open"]) * (1 - slippage_pct)

    risk_per_sh = abs(entry_price - sig.stop_loss)
    if risk_per_sh <= 0:
        return None

    # Walk forward to find exit
    exit_price  = entry_price
    exit_reason = "TIME"
    exit_date   = entry_date

    for i in range(2, len(df)):
        bar  = df.iloc[i]
        high = float(bar["High"])
        low  = float(bar["Low"])
        date = str(df.index[i].date())

        if sig.direction == "LONG":
            if low <= sig.stop_loss:
                exit_price  = sig.stop_loss
                exit_reason = "STOP"
                exit_date   = date
                break
            if high >= sig.tp2:
                exit_price  = sig.tp2
                exit_reason = "TP2"
                exit_date   = date
                break
            if high >= sig.tp1:
                exit_price  = sig.tp1
                exit_reason = "TP1"
                exit_date   = date
                break
        else:
            if high >= sig.stop_loss:
                exit_price  = sig.stop_loss
                exit_reason = "STOP"
                exit_date   = date
                break
            if low <= sig.tp2:
                exit_price  = sig.tp2
                exit_reason = "TP2"
                exit_date   = date
                break
            if low <= sig.tp1:
                exit_price  = sig.tp1
                exit_reason = "TP1"
                exit_date   = date
                break

    if exit_reason == "TIME":
        # Exit at last close (time-based)
        exit_price = float(df.iloc[-1]["Close"])
        exit_date  = str(df.index[-1].date())

    # P&L calculation
    if sig.direction == "LONG":
        pnl = (exit_price - entry_price) * sig.shares - commission * 2
    else:
        pnl = (entry_price - exit_price) * sig.shares - commission * 2

    pnl_pct   = pnl / (entry_price * sig.shares) * 100 if entry_price * sig.shares else 0
    r_multiple = pnl / (risk_per_sh * sig.shares) if risk_per_sh * sig.shares else 0

    return BacktestTrade(
        symbol      = sig.symbol,
        direction   = sig.direction,
        entry_date  = entry_date,
        exit_date   = exit_date,
        entry_price = round(entry_price, 4),
        exit_price  = round(exit_price, 4),
        stop_loss   = sig.stop_loss,
        tp1         = sig.tp1,
        tp2         = sig.tp2,
        shares      = sig.shares,
        pnl         = round(pnl, 2),
        pnl_pct     = round(pnl_pct, 2),
        exit_reason = exit_reason,
        r_multiple  = round(r_multiple, 2),
    )


# ── Stats ─────────────────────────────────────────────────────────────────────

def compute_report(trades: List[BacktestTrade]) -> BacktestReport:
    if not trades:
        return BacktestReport(trades=[])

    pnls  = [t.pnl  for t in trades]
    wins  = [p for p in pnls if p > 0]
    loss  = [p for p in pnls if p < 0]

    total   = len(pnls)
    win_rate = len(wins) / total if total else 0
    avg_win  = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(loss) / len(loss)) if loss else 0
    expect  = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    pf      = abs(sum(wins) / sum(loss)) if loss else float("inf")

    # Equity curve & drawdown
    equity  = np.cumsum(pnls)
    peak    = np.maximum.accumulate(equity)
    dd      = equity - peak
    max_dd  = float(dd.min()) if len(dd) else 0.0

    # Sharpe (annualised, assuming 252 trading days, daily returns as fraction of account)
    if len(pnls) > 1 and CONFIG.ACCOUNT_SIZE:
        daily_ret = np.array(pnls) / CONFIG.ACCOUNT_SIZE
        sharpe    = (np.mean(daily_ret) / np.std(daily_ret) * math.sqrt(252)
                     if np.std(daily_ret) > 0 else 0.0)
    else:
        sharpe = 0.0

    return BacktestReport(
        trades        = trades,
        total_trades  = total,
        wins          = len(wins),
        losses        = len(loss),
        win_rate      = round(win_rate, 4),
        avg_win       = round(avg_win, 2),
        avg_loss      = round(avg_loss, 2),
        expectancy    = round(expect, 2),
        profit_factor = round(pf, 2),
        total_pnl     = round(sum(pnls), 2),
        max_drawdown  = round(max_dd, 2),
        sharpe_ratio  = round(sharpe, 2),
        avg_r_multiple = round(sum(t.r_multiple for t in trades) / total, 2),
    )


# ── Public entry ──────────────────────────────────────────────────────────────

def backtest_signals(signals: List[BacktestSignal],
                     slippage_pct: float = 0.001,
                     commission: float = 1.0,
                     max_hold_bars: int = 5) -> BacktestReport:
    """
    Run backtest on a list of BacktestSignal objects.
    Fetches real forward price data for each.
    """
    logger.info(f"[Backtest] Running {len(signals)} signals …")
    trades = []
    for i, sig in enumerate(signals):
        t = simulate_trade(sig, slippage_pct, commission, max_hold_bars)
        if t:
            trades.append(t)
        if i % 10 == 0:
            logger.debug(f"[Backtest] Processed {i+1}/{len(signals)}")

    report = compute_report(trades)
    report.print_summary()
    return report


def backtest_from_db(days: int = 30) -> BacktestReport:
    """
    Pull stored signals from DB and replay them.
    Requires database.py to have saved historical signals.
    """
    from scanner.database import DB
    records = DB.get_recent_signals(limit=200)
    sigs = []
    for r in records:
        sigs.append(BacktestSignal(
            symbol      = r["symbol"],
            direction   = r["direction"],
            entry       = r["entry"],
            stop_loss   = r["stop_loss"],
            tp1         = r["tp1"],
            tp2         = r["tp2"],
            tp3         = r["tp3"],
            shares      = r["shares"] or 1,
            signal_date = r["fired_at"][:10],
        ))
    return backtest_signals(sigs)
