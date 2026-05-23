"""
risk.py  –  Risk Management Engine
=====================================
For each scored candidate generates a complete trade plan:
  • Entry zone
  • ATR-based stop loss
  • S/R-based stop loss  (uses the tighter of the two)
  • Take profit targets  (1R, 2R, 3R)
  • Position size        (fixed fractional risk %)
  • R/R ratio
  • Daily loss guard     (drawdown protection)

Rejects setups with R/R < MIN_RISK_REWARD.
"""

import math
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List

from scanner.config import CONFIG
from scanner.support_resistance import SRBundle
from scanner.indicators import IndicatorBundle
from scanner.scoring import SignalScore
from scanner.utils import logger


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class TradePlan:
    symbol:        str
    direction:     str      # 'LONG' | 'SHORT'
    score:         float

    entry:         float = 0.0
    stop_loss:     float = 0.0
    tp1:           float = 0.0   # 1R target
    tp2:           float = 0.0   # 2R target
    tp3:           float = 0.0   # 3R target

    risk_per_share:  float = 0.0
    reward_per_share: float = 0.0
    rr_ratio:        float = 0.0
    shares:          int   = 0
    risk_dollars:    float = 0.0
    position_size:   float = 0.0  # USD notional

    stop_method:     str   = ""    # 'atr' | 'sr' | 'combined'
    flags:           List[str] = field(default_factory=list)
    valid:           bool  = False
    reject_reason:   str   = ""

    def summary(self) -> str:
        if not self.valid:
            return f"{self.symbol} REJECTED: {self.reject_reason}"
        return (f"{self.symbol} {self.direction} | "
                f"Entry={self.entry:.2f} SL={self.stop_loss:.2f} "
                f"TP1={self.tp1:.2f} TP2={self.tp2:.2f} | "
                f"R/R={self.rr_ratio:.1f} | "
                f"Size={self.shares}sh ${self.position_size:.0f} | "
                f"Risk=${self.risk_dollars:.0f}")


@dataclass
class PortfolioRisk:
    """Tracks live portfolio-level risk exposure."""
    account_size:      float
    daily_loss_budget: float      # max loss allowed today
    daily_loss_used:   float = 0.0
    open_positions:    int   = 0
    sector_exposure:   dict  = field(default_factory=dict)

    def can_take_trade(self, risk_dollars: float, sector: str = "") -> tuple:
        """Returns (ok: bool, reason: str)."""
        if self.daily_loss_used + risk_dollars > self.daily_loss_budget:
            return False, f"Daily loss limit reached (${self.daily_loss_budget:.0f})"
        if sector:
            current = self.sector_exposure.get(sector, 0.0)
            if current + risk_dollars > self.account_size * CONFIG.MAX_SECTOR_EXPOSURE_PCT:
                return False, f"Sector exposure limit for {sector}"
        return True, "ok"

    def record_trade(self, risk_dollars: float, sector: str = ""):
        self.daily_loss_used += risk_dollars
        if sector:
            self.sector_exposure[sector] = self.sector_exposure.get(sector, 0) + risk_dollars
        self.open_positions += 1


# ── Stop loss calculation ─────────────────────────────────────────────────────

def _atr_stop(price: float, atr_val: float, direction: str,
              multiplier: float = None) -> float:
    """ATR-based stop: price ± ATR × multiplier."""
    mult = multiplier or CONFIG.ATR_STOP_MULTIPLIER
    if np.isnan(atr_val) or atr_val <= 0:
        return price * (0.97 if direction == "LONG" else 1.03)
    stop = price - (atr_val * mult) if direction == "LONG" else price + (atr_val * mult)
    return round(stop, 4)


def _sr_stop(price: float, direction: str, sr: Optional[SRBundle],
             buffer_pct: float = 0.002) -> Optional[float]:
    """Place stop just below nearest support (LONG) or above resistance (SHORT)."""
    if not sr:
        return None
    if direction == "LONG" and sr.nearest_support:
        return round(sr.nearest_support * (1 - buffer_pct), 4)
    if direction == "SHORT" and sr.nearest_resistance:
        return round(sr.nearest_resistance * (1 + buffer_pct), 4)
    return None


def _best_stop(price: float, direction: str,
               atr_val: float, sr: Optional[SRBundle]) -> tuple:
    """
    Returns (stop_price, method_used).
    Uses tighter stop of ATR vs S/R – tighter = less risk, better R/R.
    """
    atr_s = _atr_stop(price, atr_val, direction)
    sr_s  = _sr_stop(price, direction, sr)

    if sr_s is None:
        return atr_s, "atr"

    if direction == "LONG":
        # Tighter = higher stop for longs
        stop = max(atr_s, sr_s)
        method = "sr" if stop == sr_s else "atr"
    else:
        # Tighter = lower stop for shorts
        stop = min(atr_s, sr_s)
        method = "sr" if stop == sr_s else "atr"

    return stop, f"combined({method})"


# ── Position sizing ───────────────────────────────────────────────────────────

def _position_size(account: float, risk_pct: float,
                   entry: float, stop: float) -> tuple:
    """
    Fixed fractional position sizing.
    Returns (shares: int, risk_dollars: float, position_dollars: float).
    """
    risk_dollars = account * risk_pct
    risk_per_sh  = abs(entry - stop)
    if risk_per_sh <= 0:
        return 0, 0.0, 0.0
    shares   = math.floor(risk_dollars / risk_per_sh)
    pos_size = shares * entry
    return shares, round(risk_dollars, 2), round(pos_size, 2)


# ── Targets ───────────────────────────────────────────────────────────────────

def _take_profits(entry: float, stop: float, direction: str) -> tuple:
    """Returns (tp1, tp2, tp3) = 1R, 2R, 3R targets."""
    risk = abs(entry - stop)
    if direction == "LONG":
        return (round(entry + risk, 4),
                round(entry + risk * 2, 4),
                round(entry + risk * 3, 4))
    else:
        return (round(entry - risk, 4),
                round(entry - risk * 2, 4),
                round(entry - risk * 3, 4))


# ── Master plan builder ───────────────────────────────────────────────────────

def build_trade_plan(
    sig:       SignalScore,
    ind:       IndicatorBundle,
    sr:        Optional[SRBundle],
    price:     float,
    cfg=CONFIG,
    portfolio: Optional[PortfolioRisk] = None,
    sector:    str = "",
) -> TradePlan:
    """
    Build and validate a full trade plan for one signal.
    Returns TradePlan with valid=True only if setup passes all risk checks.
    """
    plan = TradePlan(
        symbol    = sig.symbol,
        direction = sig.direction,
        score     = sig.composite,
        flags     = sig.flags.copy(),
    )

    # ── Entry zone ──
    # Enter at current price (market-on-open / limit at ask)
    plan.entry = round(price, 4)

    # ── Stop loss ──
    atr_val = ind.atr_val if not np.isnan(ind.atr_val) else price * 0.02
    plan.stop_loss, plan.stop_method = _best_stop(price, sig.direction, atr_val, sr)

    # Sanity: stop shouldn't be on the wrong side
    if sig.direction == "LONG" and plan.stop_loss >= plan.entry:
        plan.valid = False
        plan.reject_reason = "Invalid stop (≥ entry)"
        return plan
    if sig.direction == "SHORT" and plan.stop_loss <= plan.entry:
        plan.valid = False
        plan.reject_reason = "Invalid stop (≤ entry for short)"
        return plan

    # ── Risk per share & R/R ──
    plan.risk_per_share    = abs(plan.entry - plan.stop_loss)
    plan.tp1, plan.tp2, plan.tp3 = _take_profits(plan.entry, plan.stop_loss, sig.direction)
    plan.reward_per_share  = abs(plan.tp2 - plan.entry)  # use 2R as "primary" target
    plan.rr_ratio = round(
        plan.reward_per_share / plan.risk_per_share
        if plan.risk_per_share else 0, 2)

    # Reject poor R/R
    if plan.rr_ratio < cfg.MIN_RISK_REWARD:
        plan.valid = False
        plan.reject_reason = f"R/R={plan.rr_ratio:.1f} < min {cfg.MIN_RISK_REWARD}"
        return plan

    # ── Position sizing ──
    plan.shares, plan.risk_dollars, plan.position_size = _position_size(
        cfg.ACCOUNT_SIZE, cfg.MAX_RISK_PER_TRADE_PCT, plan.entry, plan.stop_loss)

    if plan.shares == 0:
        plan.valid = False
        plan.reject_reason = "Position size = 0 (stop too wide)"
        return plan

    # ── Portfolio-level guard ──
    if portfolio:
        ok, reason = portfolio.can_take_trade(plan.risk_dollars, sector)
        if not ok:
            plan.valid = False
            plan.reject_reason = f"Portfolio limit: {reason}"
            return plan

    plan.valid = True
    return plan


# ── Utility: Kelly Criterion ──────────────────────────────────────────────────

def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Kelly Criterion: f* = (bp - q) / b
    where b = avg_win/avg_loss, p = win_rate, q = 1 - p.
    Returns recommended fraction of capital to risk (capped at 25%).
    """
    if avg_loss == 0:
        return 0.0
    b = avg_win / avg_loss
    q = 1 - win_rate
    k = (b * win_rate - q) / b
    return round(max(0.0, min(k, 0.25)), 4)


# ── Volatility parity sizing ──────────────────────────────────────────────────

def vol_parity_size(account: float, target_vol: float,
                    atr_pct: float, entry: float) -> int:
    """
    Size positions so each contributes equally to portfolio volatility.
    target_vol: desired daily $ volatility contribution per position.
    atr_pct:    ATR as % of price (daily expected move).
    """
    if atr_pct <= 0:
        return 0
    daily_vol_per_share = entry * (atr_pct / 100)
    shares = math.floor(target_vol / daily_vol_per_share)
    return max(0, shares)
