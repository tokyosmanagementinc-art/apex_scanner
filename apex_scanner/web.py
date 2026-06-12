import socket
from pathlib import Path

from flask import Flask, render_template, request, jsonify

from scanner.background import get_cached_state, start_background_scanner, start_background_scanner_thread, SESSION_TYPES
from scanner.config import CONFIG
from scanner.utils import fmt_price, logger, get_market_session

app = Flask(
    __name__,
    template_folder=str(Path(__file__).resolve().parent / "templates"),
)


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


def _public_state(raw_state: dict) -> dict:
    state = raw_state.copy()
    results = state.get("results", []) or []
    # Removed most-active / unusual-volume lists — keep only primary results
    state["selected_session"] = state.get("session")
    state["current_market_session"] = get_market_session()
    if state.get("next_run_in") is not None:
        state["estimated_remaining"] = state["next_run_in"]
    return state


@app.route("/", methods=["GET"])
def index():
    session = _selected_session()
    state = _public_state(get_cached_state(session))
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
    return jsonify(_public_state(get_cached_state(session)))


@app.route("/health", methods=["GET"])
def health():
    state = get_cached_state(_selected_session())
    return {
        "status": "ok",
        "running": state.get("running", False),
        "selected_session": state.get("session"),
        "current_market_session": get_market_session(),
    }


def init_scanner_daemon(use_process: bool = True) -> None:
    """Initialize background scanner. By default starts as a separate process.
    Pass `use_process=False` to run scanner in a thread within this process (logs visible here).
    """
    if use_process:
        start_background_scanner()
    else:
        # start in-process thread for easier debugging and visible logs
        try:
            start_background_scanner_thread()
        except Exception:
            # fall back to process if thread starter not available
            start_background_scanner()


if __name__ == "__main__":
    init_scanner_daemon()
    port = find_free_port(8000)
    logger.info(f"Starting web dashboard on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
