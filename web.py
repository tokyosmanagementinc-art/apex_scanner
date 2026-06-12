<<<<<<< HEAD
import socket
from typing import Optional

from flask import Flask, render_template, request, jsonify

from scanner.background import (
    get_cached_state,
    start_background_scanner,
    start_background_scanner_thread,
    SESSION_TYPES,
    set_forced_session,
    set_forced_setup_type,
)
from scanner.config import CONFIG
from scanner.universe import _get_filter_thresholds
from scanner.utils import fmt_price, logger, get_market_session, CACHE

app = Flask(__name__, template_folder="templates")


def _is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def find_free_port(start_port: int = 8000, max_port: int = 8100) -> int:
    for port in range(start_port, max_port):
        if _is_port_available(port):
            return port
    raise RuntimeError(f"No free ports found between {start_port} and {max_port - 1}")


def _selected_session() -> str:
    requested = request.args.get("session", "").lower()
    if requested in SESSION_TYPES:
        return requested
    return get_market_session()


def _selected_setup_type() -> Optional[str]:
    requested = request.args.get("setup_type")
    if requested is not None:
        requested = requested.lower()
        return requested if requested in ("day", "swing") else None
    # fallback to persisted override saved in file cache
    overrides = CACHE.get("apex_scan_overrides") or {}
    if isinstance(overrides, dict):
        s = overrides.get("setup_type")
        if s in ("day", "swing"):
            return s
    return None


def _public_state(raw_state: dict, setup_type: Optional[str] = None) -> dict:
    state = raw_state.copy()
    results = state.get("results", []) or []
    # Removed most-active / unusual-volume lists — keep only primary results
    state["selected_session"] = state.get("session")
    state["current_market_session"] = get_market_session()
    state["selected_setup_type"] = setup_type or state.get("setup_type") or "day"
    state["selected_setup_type"] = state["selected_setup_type"] if state["selected_setup_type"] in ("day", "swing") else "day"
    if state.get("next_run_in") is not None:
        state["estimated_remaining"] = state["next_run_in"]
    # Provide effective filters for the UI based on selected session + setup type
    sel = state.get("session") or state.get("current_market_session") or "regular"
    sel = sel if sel in SESSION_TYPES else "regular"
    thresholds = _get_filter_thresholds(sel, state["selected_setup_type"], CONFIG)
    state["filters"] = {
        "MIN_PRICE": CONFIG.MIN_PRICE,
        "MAX_PRICE": CONFIG.MAX_PRICE,
        "MIN_VOLUME": thresholds["min_volume"],
        "MIN_REL_VOLUME": thresholds["min_rel_volume"],
        "MIN_MARKET_CAP": thresholds["min_market_cap"],
        "MIN_GAP_PCT": thresholds["min_gap_pct"],
        "MIN_CHANGE_PCT": thresholds["min_change_pct"],
    }
    return state


@app.route("/", methods=["GET"])
def index():
    session = _selected_session()
    setup_type = _selected_setup_type()
    state = _public_state(get_cached_state(session), setup_type=setup_type)
    return render_template(
        "dashboard.html",
        state=state,
        state_json=state,
        config=CONFIG,
        fmt_price=fmt_price,
    )


@app.route("/state", methods=["GET"])
def state():
    session = _selected_session()
    setup_type = _selected_setup_type()
    return jsonify(_public_state(get_cached_state(session), setup_type=setup_type))


@app.route("/health", methods=["GET"])
def health():
    session = _selected_session()
    setup_type = _selected_setup_type()
    state = get_cached_state(session)
    return {
        "status": "ok",
        "running": state.get("running", False),
        "selected_session": state.get("session"),
        "selected_setup_type": setup_type or state.get("setup_type") or "day",
        "current_market_session": get_market_session(),
    }
from apex_scanner.web import app, init_scanner_daemon


if __name__ == "__main__":
    init_scanner_daemon()
    app.run(host="0.0.0.0", port=8000, debug=False)
        set_forced_session(requested)
