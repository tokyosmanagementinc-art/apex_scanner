import multiprocessing
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

from scanner.config import CONFIG
from scanner.scanner import run_full_scan, ScanResult
from scanner.universe import UniverseStats
from scanner.utils import CACHE, get_market_session, logger

SESSION_TYPES = ("regular", "pre-market", "after-hours")
CACHE_KEY = "apex_scan_cache_v1"
CACHE_TTL = 86_400

_SCAN_CACHE_LOCK = threading.RLock()
_SCAN_LOCK = threading.Lock()
_SCAN_PROCESS: Optional[multiprocessing.Process] = None
_SCAN_PROCESS_STARTED = False
_SCAN_THREAD_STARTED = False
_SCAN_THREAD: Optional[threading.Thread] = None

SESSION_CACHE: Dict[str, dict] = {}


def _iso_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _default_state(session: str) -> dict:
    return {
        "session": session,
        "current_market_session": get_market_session(),
        "status": "Idle",
        "running": False,
        "cancel_requested": False,
        "phase": "Waiting",
        "phase_message": "Background scanner is waiting to run.",
        "progress": {"current": 0, "total": 0},
        "last_scan": None,
        "scan_started_at": None,
        "elapsed_seconds": None,
        "last_duration": None,
        "last_updated": _iso_now(),
        "next_run_in": None,
        "error": None,
        "results": [],
        "regime": None,
        "stats": None,
        "logs": [],
        "freshness_seconds": None,
    }


def _compute_elapsed(state: dict) -> Optional[int]:
    started_at = state.get("scan_started_at")
    if not started_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        return max(0, int((datetime.utcnow() - start).total_seconds()))
    except Exception:
        return None


def _load_cache() -> Dict[str, dict]:
    raw = CACHE.get(CACHE_KEY) or {}
    data = {}
    for session in SESSION_TYPES:
        default = _default_state(session)
        loaded = raw.get(session, {}) if isinstance(raw, dict) else {}
        default.update(loaded)
        data[session] = default
    return data


def _save_cache() -> None:
    with _SCAN_CACHE_LOCK:
        try:
            CACHE.set(CACHE_KEY, SESSION_CACHE, ttl=CACHE_TTL)
        except Exception as exc:
            logger.warning(f"Background cache save failed: {exc}")


def _append_log(session: str, message: str) -> None:
    with _SCAN_CACHE_LOCK:
        state = SESSION_CACHE.setdefault(session, _default_state(session))
        entry = {"time": _iso_now(), "message": message}
        if not state["logs"] or state["logs"][0]["message"] != message:
            state["logs"].insert(0, entry)
            state["logs"] = state["logs"][:80]
        state["last_updated"] = _iso_now()
        _save_cache()


def _update_state(session: str, updates: dict) -> None:
    with _SCAN_CACHE_LOCK:
        state = SESSION_CACHE.setdefault(session, _default_state(session))
        state.update(updates)
        state["current_market_session"] = get_market_session()
        state["last_updated"] = _iso_now()
        if state.get("last_scan"):
            try:
                ts = datetime.fromisoformat(state["last_scan"].replace("Z", "+00:00"))
                state["freshness_seconds"] = int((datetime.utcnow() - ts).total_seconds())
            except Exception:
                state["freshness_seconds"] = None
        else:
            state["freshness_seconds"] = None

        if state.get("running"):
            state["elapsed_seconds"] = _compute_elapsed(state)
        elif state.get("scan_started_at") and state.get("last_duration") is None:
            state["elapsed_seconds"] = _compute_elapsed(state)

        _save_cache()


def _serialize_result(result: ScanResult) -> dict:
    stock = result.stock
    signal = result.signal
    plan = result.plan
    return {
        "symbol": stock.symbol,
        "company": getattr(stock, "name", ""),
        "price": getattr(result, "price", 0.0),
        "change_pct": getattr(stock, "change_pct", 0.0),
        "rel_volume": getattr(stock, "rel_volume", 0.0),
        "market_cap": getattr(stock, "market_cap", 0.0),
        "signal_direction": getattr(signal, "direction", ""),
        "confidence": getattr(signal, "confidence", ""),
        "composite": getattr(signal, "composite", 0.0),
        "entry": getattr(plan, "entry", 0.0),
        "stop_loss": getattr(plan, "stop_loss", 0.0),
        "tp1": getattr(plan, "tp1", 0.0),
        "tp2": getattr(plan, "tp2", 0.0),
        "rr_ratio": getattr(plan, "rr_ratio", 0.0),
        "risk_dollars": getattr(plan, "risk_dollars", 0.0),
        "setup_type": getattr(signal, "setup_type", "Standard"),
        "flags": getattr(signal, "flags", []),
        "trend": getattr(signal, "trend", ""),
    }


def _serialize_regime(regime) -> Optional[dict]:
    if not regime:
        return None
    return {
        "classification": getattr(regime, "classification", None),
        "spy_vs_50sma": getattr(regime, "spy_vs_50sma", None),
        "spy_vs_200sma": getattr(regime, "spy_vs_200sma", None),
        "vix": getattr(regime, "vix", None),
        "vix_regime": getattr(regime, "vix_regime", None),
        "top_sector": getattr(regime, "top_sector", None),
        "bot_sector": getattr(regime, "bot_sector", None),
    }


def _serialize_stats(stats: Optional[UniverseStats]) -> Optional[dict]:
    if not stats:
        return None
    try:
        return vars(stats)
    except Exception:
        return None


def get_cached_state(session: str) -> dict:
    session = session if session in SESSION_TYPES else get_market_session()
    data = _load_cache()
    state = data.get(session, _default_state(session)).copy()
    state["current_market_session"] = get_market_session()
    if state.get("last_scan"):
        try:
            ts = datetime.fromisoformat(state["last_scan"].replace("Z", "+00:00"))
            state["freshness_seconds"] = int((datetime.utcnow() - ts).total_seconds())
        except Exception:
            state["freshness_seconds"] = None
    else:
        state["freshness_seconds"] = None
    if state.get("next_run_at"):
        state["next_run_in"] = max(0, int(state["next_run_at"] - time.time()))
    return state


def _run_scan(session: str) -> None:
    if _SCAN_LOCK.locked():
        _append_log(session, "Scan skipped because a scan is already running.")
        return

    _update_state(session, {
        "status": "Scanning",
        "running": True,
        "error": None,
        "phase": "Discovering universe",
        "phase_message": f"Running background scan for {session} session.",
        "progress": {"current": 0, "total": 0},
        "scan_started_at": _iso_now(),
        "elapsed_seconds": 0,
        "last_duration": None,
    })
    _append_log(session, f"Background scan started for {session}.")

    def stage_callback(stats: UniverseStats) -> None:
        _update_state(session, {
            "phase": "Ranking setups",
            "phase_message": f"Found {stats.stage2_passed or 0} active candidates for ranking.",
            "stats": _serialize_stats(stats),
        })

    def progress_callback(processed: int, total: int) -> None:
        _update_state(session, {
            "phase": "Analyzing candidates",
            "phase_message": f"Analyzing {processed}/{total} candidates…",
            "progress": {"current": processed, "total": total},
        })

    try:
        with _SCAN_LOCK:
            results, regime, stats = run_full_scan(
                force_universe_refresh=False,
                market_session=session,
                cancel_event=None,
                result_callback=None,
                progress_callback=progress_callback,
                stage_callback=stage_callback,
            )
            completed_duration = _compute_elapsed(SESSION_CACHE[session]) if SESSION_CACHE[session].get("scan_started_at") else None
            _update_state(session, {
                "status": "Ready",
                "running": False,
                "phase": "Scan complete",
                "phase_message": f"Scan complete with {len(results)} top setups.",
                "results": [_serialize_result(r) for r in results],
                "regime": _serialize_regime(regime),
                "stats": _serialize_stats(stats),
                "last_scan": _iso_now(),
                "elapsed_seconds": completed_duration,
                "last_duration": completed_duration,
                "error": None,
            })
            _append_log(session, f"Background scan completed for {session}.")
    except Exception as exc:
        logger.exception("Background scan failed")
        _update_state(session, {
            "status": "Error",
            "running": False,
            "phase": "Error",
            "phase_message": f"Last scan failed: {exc}",
            "error": str(exc),
        })
        _append_log(session, f"Background scan failed: {exc}")


def _session_interval(session: str) -> int:
    return CONFIG.SCAN_INTERVAL_MIN if session == "regular" else CONFIG.SCAN_INTERVAL_MIN_EXTENDED


def background_scan_loop() -> None:
    global SESSION_CACHE
    SESSION_CACHE = _load_cache()
    while True:
        session = get_market_session()
        interval = _session_interval(session) * 60
        next_run_at = time.time() + interval
        _update_state(session, {"next_run_at": next_run_at})
        try:
            _run_scan(session)
        except Exception:
            pass
        sleep_seconds = max(5, next_run_at - time.time())
        time.sleep(sleep_seconds)


def start_background_scanner() -> None:
    global _SCAN_PROCESS_STARTED, _SCAN_PROCESS
    if _SCAN_PROCESS_STARTED:
        return
    _SCAN_PROCESS_STARTED = True
    _SCAN_PROCESS = multiprocessing.Process(target=background_scan_loop, daemon=True)
    _SCAN_PROCESS.start()
    logger.info(f"Background scanner process started: pid={_SCAN_PROCESS.pid}")


def start_background_scanner_thread() -> None:
    """Start the background scanner in a local daemon thread (logs appear in this terminal)."""
    global _SCAN_THREAD_STARTED, _SCAN_THREAD
    try:
        _SCAN_THREAD_STARTED
    except NameError:
        _SCAN_THREAD_STARTED = False
        _SCAN_THREAD = None
    if _SCAN_THREAD_STARTED:
        return
    _SCAN_THREAD_STARTED = True
    _SCAN_THREAD = threading.Thread(target=background_scan_loop, daemon=True)
    _SCAN_THREAD.start()
    logger.info(f"Background scanner thread started: name={_SCAN_THREAD.name}")
