"""
GUI runner for macOS app bundle.
Starts the Flask dashboard and automatically opens it in the default browser.
"""
import os
import sys
import time
import webbrowser
import threading
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from apex_scanner.web import app, init_scanner_daemon
from apex_scanner.main import find_free_port


def start_app():
    """Start the Flask dashboard and open browser."""
    # Start background scanner in a thread
    try:
        init_scanner_daemon(use_process=False)  # Use thread for GUI app
    except Exception as e:
        print(f"Warning: Could not start background scanner: {e}")

    # Find a free port
    port = find_free_port(8000, 8100)
    url = f"http://localhost:{port}"

    # Start Flask in a thread
    def run_flask():
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Wait a moment for Flask to start, then open browser
    time.sleep(2)
    try:
        webbrowser.open(url)
        print(f"Dashboard opened at {url}")
    except Exception as e:
        print(f"Could not open browser: {e}")
        print(f"Visit {url} manually")

    # Keep the app running
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    start_app()
