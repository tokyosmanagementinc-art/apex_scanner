"""
database.py  –  SQLite Persistence Layer
==========================================
Tables:
  scans     – each full scan run
  signals   – every trade signal emitted
  trades    – manual trade journal (for performance tracking)
  performance – running P&L metrics

SQLite for development; swap engine URL for PostgreSQL in production.
"""

import json
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from scanner.config import CONFIG
from scanner.utils import logger


# ── Schema ─────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT    NOT NULL,
    regime        TEXT,
    universe_size INTEGER,
    candidates    INTEGER,
    top_picks     INTEGER,
    duration_sec  REAL,
    metadata      TEXT                    -- JSON
);

CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id       INTEGER REFERENCES scans(id),
    symbol        TEXT    NOT NULL,
    direction     TEXT,
    score         REAL,
    confidence    TEXT,
    price         REAL,
    entry         REAL,
    stop_loss     REAL,
    tp1           REAL,
    tp2           REAL,
    tp3           REAL,
    rr_ratio      REAL,
    shares        INTEGER,
    risk_dollars  REAL,
    flags         TEXT,   -- JSON array
    components    TEXT,   -- JSON object
    fired_at      TEXT    NOT NULL,
    outcome       TEXT,   -- 'win'|'loss'|'scratch'|NULL
    pnl           REAL
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER REFERENCES signals(id),
    symbol        TEXT    NOT NULL,
    direction     TEXT,
    entry_price   REAL,
    exit_price    REAL,
    shares        INTEGER,
    pnl           REAL,
    pnl_pct       REAL,
    hold_minutes  INTEGER,
    entry_at      TEXT,
    exit_at       TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS performance (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT UNIQUE NOT NULL,
    trades        INTEGER DEFAULT 0,
    wins          INTEGER DEFAULT 0,
    losses        INTEGER DEFAULT 0,
    gross_pnl     REAL    DEFAULT 0.0,
    net_pnl       REAL    DEFAULT 0.0,
    win_rate      REAL    DEFAULT 0.0,
    avg_win       REAL    DEFAULT 0.0,
    avg_loss      REAL    DEFAULT 0.0,
    expectancy    REAL    DEFAULT 0.0,
    profit_factor REAL    DEFAULT 0.0
);
"""


# ── Database class ─────────────────────────────────────────────────────────────

class Database:
    def __init__(self, path: str = None):
        self.path = path or CONFIG.DB_PATH
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        logger.debug(f"[DB] Schema ready at {self.path}")

    # ── Scans ──

    def insert_scan(self, regime: str, universe_size: int, candidates: int,
                    top_picks: int, duration_sec: float,
                    metadata: dict = None) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO scans (run_at,regime,universe_size,candidates,top_picks,duration_sec,metadata) "
                "VALUES (?,?,?,?,?,?,?)",
                (datetime.now().isoformat(), regime, universe_size, candidates,
                 top_picks, duration_sec, json.dumps(metadata or {}))
            )
            return cur.lastrowid

    # ── Signals ──

    def insert_signal(self, scan_id: int, plan, sig, price: float) -> int:
        flags = json.dumps(sig.flags)
        comps = json.dumps({c.name: round(c.raw_score, 3)
                            for c in sig.components})
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO signals
                   (scan_id,symbol,direction,score,confidence,price,entry,stop_loss,
                    tp1,tp2,tp3,rr_ratio,shares,risk_dollars,flags,components,fired_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (scan_id, sig.symbol, sig.direction, sig.composite, sig.confidence,
                 price, plan.entry, plan.stop_loss, plan.tp1, plan.tp2, plan.tp3,
                 plan.rr_ratio, plan.shares, plan.risk_dollars, flags, comps,
                 datetime.now().isoformat())
            )
            return cur.lastrowid

    def update_signal_outcome(self, signal_id: int, outcome: str, pnl: float) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE signals SET outcome=?,pnl=? WHERE id=?",
                         (outcome, pnl, signal_id))

    def get_recent_signals(self, limit: int = 50) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY fired_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Trades ──

    def record_trade(self, signal_id: Optional[int], symbol: str,
                     direction: str, entry: float, exit_: float,
                     shares: int, entry_at: str, exit_at: str,
                     notes: str = "") -> None:
        pnl = (exit_ - entry) * shares if direction == "LONG" else (entry - exit_) * shares
        pnl_pct = pnl / (entry * shares) * 100 if entry * shares else 0
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO trades
                   (signal_id,symbol,direction,entry_price,exit_price,shares,
                    pnl,pnl_pct,entry_at,exit_at,notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (signal_id, symbol, direction, entry, exit_, shares,
                 round(pnl, 2), round(pnl_pct, 2), entry_at, exit_at, notes)
            )

    # ── Performance ──

    def refresh_daily_performance(self, date_str: str = None) -> None:
        today = date_str or datetime.now().strftime("%Y-%m-%d")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT pnl FROM trades WHERE entry_at LIKE ?", (f"{today}%",)
            ).fetchall()

        pnls = [r["pnl"] for r in rows]
        if not pnls:
            return

        total   = len(pnls)
        wins    = [p for p in pnls if p > 0]
        losses  = [p for p in pnls if p < 0]
        gross   = sum(pnls)
        wr      = len(wins) / total if total else 0
        avg_w   = sum(wins)  / len(wins)  if wins   else 0
        avg_l   = abs(sum(losses) / len(losses)) if losses else 0
        exp     = (wr * avg_w) - ((1 - wr) * avg_l)
        pf      = abs(sum(wins) / sum(losses)) if losses else float("inf")

        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO performance
                   (date,trades,wins,losses,gross_pnl,net_pnl,win_rate,
                    avg_win,avg_loss,expectancy,profit_factor)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (today, total, len(wins), len(losses), round(gross, 2),
                 round(gross, 2), round(wr, 4), round(avg_w, 2),
                 round(avg_l, 2), round(exp, 2), round(pf, 2))
            )

    def get_performance_summary(self, days: int = 30) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM performance ORDER BY date DESC LIMIT ?", (days,)
            ).fetchall()
        return [dict(r) for r in rows]

    def lifetime_stats(self) -> Dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as trades,
                          SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
                          SUM(pnl) as total_pnl,
                          AVG(CASE WHEN pnl>0 THEN pnl END) as avg_win,
                          AVG(CASE WHEN pnl<0 THEN pnl END) as avg_loss
                   FROM trades"""
            ).fetchone()
        d = dict(row)
        if d["trades"]:
            d["win_rate"] = d["wins"] / d["trades"]
        return d


# Singleton
DB = Database()
