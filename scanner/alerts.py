"""
alerts.py  –  Multi-channel Alert Dispatcher
==============================================
Channels (all free):
  • Console / terminal  (always on)
  • Discord webhook     (set DISCORD_WEBHOOK in .env)
  • Telegram bot        (set TELEGRAM_TOKEN + TELEGRAM_CHAT_ID in .env)

Only fires when score ≥ MIN_SCORE_TO_ALERT (configurable).
"""

import json
import logging
import requests
from typing import List, Optional

from scanner.config import CONFIG
from scanner.risk import TradePlan
from scanner.scoring import SignalScore
from scanner.utils import logger, fmt_price, fmt_pct, now_str


# ── Alert message builder ─────────────────────────────────────────────────────

def _build_message(plan: TradePlan, sig: SignalScore, price: float) -> str:
    direction_emoji = "🟢" if plan.direction == "LONG" else "🔴"
    conf_emoji = {"VERY HIGH": "🔥", "HIGH": "⚡", "MEDIUM": "📊", "LOW": "👁️"}.get(
        sig.confidence, "📊")

    flags_str = "  •  ".join(sig.flags) if sig.flags else "—"

    return (
        f"\n{'='*52}\n"
        f"{direction_emoji}  {plan.symbol}  {plan.direction}  |  Score: {plan.score:.0f}/100  {conf_emoji}\n"
        f"{'='*52}\n"
        f"  Confidence   : {sig.confidence}\n"
        f"  Price        : {fmt_price(price)}\n"
        f"  Entry Zone   : {fmt_price(plan.entry)}\n"
        f"  Stop Loss    : {fmt_price(plan.stop_loss)}  [{plan.stop_method}]\n"
        f"  TP1 (1R)     : {fmt_price(plan.tp1)}\n"
        f"  TP2 (2R)     : {fmt_price(plan.tp2)}\n"
        f"  TP3 (3R)     : {fmt_price(plan.tp3)}\n"
        f"  R/R Ratio    : {plan.rr_ratio:.1f}x\n"
        f"  Position     : {plan.shares} shares  (${plan.position_size:,.0f})\n"
        f"  Risk         : ${plan.risk_dollars:.2f}  ({CONFIG.MAX_RISK_PER_TRADE_PCT*100:.1f}%)\n"
        f"  Flags        : {flags_str}\n"
        f"  Scanned at   : {now_str()}\n"
        f"{'='*52}\n"
    )

def _build_discord_embed(plan: TradePlan, sig: SignalScore, price: float) -> dict:
    color = 0x00FF88 if plan.direction == "LONG" else 0xFF3355
    return {
        "embeds": [{
            "title": f"{'🟢' if plan.direction=='LONG' else '🔴'} {plan.symbol}  {plan.direction}  —  Score {plan.score:.0f}/100",
            "color": color,
            "fields": [
                {"name": "Entry",      "value": fmt_price(plan.entry),     "inline": True},
                {"name": "Stop Loss",  "value": fmt_price(plan.stop_loss), "inline": True},
                {"name": "R/R",        "value": f"{plan.rr_ratio:.1f}×",   "inline": True},
                {"name": "TP1 (1R)",   "value": fmt_price(plan.tp1),       "inline": True},
                {"name": "TP2 (2R)",   "value": fmt_price(plan.tp2),       "inline": True},
                {"name": "TP3 (3R)",   "value": fmt_price(plan.tp3),       "inline": True},
                {"name": "Shares",     "value": str(plan.shares),          "inline": True},
                {"name": "Risk $",     "value": f"${plan.risk_dollars:.2f}","inline": True},
                {"name": "Confidence", "value": sig.confidence,            "inline": True},
                {"name": "Flags",      "value": ", ".join(sig.flags) or "—","inline": False},
            ],
            "footer": {"text": f"APEX Scanner  •  {now_str()}"},
        }]
    }


# ── Channel senders ────────────────────────────────────────────────────────────

def _send_discord(plan: TradePlan, sig: SignalScore, price: float) -> bool:
    webhook = CONFIG.DISCORD_WEBHOOK
    if not webhook:
        return False
    try:
        payload = _build_discord_embed(plan, sig, price)
        r = requests.post(webhook, json=payload, timeout=8)
        return r.status_code in (200, 204)
    except Exception as e:
        logger.warning(f"[Alerts] Discord send failed: {e}")
        return False


def _send_telegram(plan: TradePlan, sig: SignalScore, price: float) -> bool:
    token   = CONFIG.TELEGRAM_TOKEN
    chat_id = CONFIG.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False
    try:
        text = _build_message(plan, sig, price).replace("=", "—")
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML"
        }, timeout=8)
        return r.ok
    except Exception as e:
        logger.warning(f"[Alerts] Telegram send failed: {e}")
        return False


# ── Public dispatcher ─────────────────────────────────────────────────────────

def send_alert(plan: TradePlan, sig: SignalScore, price: float,
               force: bool = False) -> None:
    """
    Send alert to all configured channels.
    Only fires when plan.score >= MIN_SCORE_TO_ALERT (unless force=True).
    """
    if not force and plan.score < CONFIG.MIN_SCORE_TO_ALERT:
        return

    msg = _build_message(plan, sig, price)
    # Console always
    print(msg)
    logger.info(f"[Alert] Fired for {plan.symbol} score={plan.score:.0f}")

    # Optional channels
    if CONFIG.DISCORD_WEBHOOK:
        ok = _send_discord(plan, sig, price)
        logger.debug(f"[Alert] Discord: {'ok' if ok else 'failed'}")

    if CONFIG.TELEGRAM_TOKEN:
        ok = _send_telegram(plan, sig, price)
        logger.debug(f"[Alert] Telegram: {'ok' if ok else 'failed'}")


def send_batch_alerts(results: list, top_n: int = 5) -> None:
    """Fire alerts for top-N results from a scan batch."""
    for item in results[:top_n]:
        if item.get("plan") and item.get("signal"):
            send_alert(item["plan"], item["signal"], item["price"])
