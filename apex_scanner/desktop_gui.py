"""
PyQt5 desktop GUI for APEX Scanner.
Displays scanner results and controls in a native macOS/Windows/Linux window.
"""
import sys
import threading
import json
from pathlib import Path
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QLabel, QPushButton, QComboBox,
    QTabWidget, QStatusBar, QScrollArea, QDialog, QDialogButtonBox,
    QFormLayout, QCheckBox
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QFont, QColor

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.background import get_cached_state, start_background_scanner_thread, set_forced_session
from scanner.market_regime import get_market_regime
from scanner.utils import fmt_price
from scanner.config import CONFIG

# Lazy imports for plotting
try:
    import pyqtgraph as pg
    from pyqtgraph import PlotWidget
    import yfinance as yf
except Exception:
    pg = None
    PlotWidget = None
    yf = None


class DetailsDialog(QDialog):
    """Dialog that shows details and a live-updating price chart using pyqtgraph."""
    def __init__(self, parent, symbol, details, refresh_interval=30):
        super().__init__(parent)
        self.setWindowTitle(f"Details — {symbol}")
        self.symbol = symbol
        self.details = details or {}
        self.refresh_interval = refresh_interval

        self.layout = QVBoxLayout()
        form = QFormLayout()
        for k, v in (self.details.items() if isinstance(self.details, dict) else []):
            form.addRow(QLabel(str(k)), QLabel(str(v)))
        self.layout.addLayout(form)

        self.plot_widget = None
        self.plot_curve = None

        if pg is not None and PlotWidget is not None and yf is not None:
            try:
                self.plot_widget = PlotWidget()
                self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
                self.plot_widget.setBackground('#ffffff')
                self.plot_curve = self.plot_widget.plot([], [], pen=pg.mkPen('#1f77b4', width=2))
                self.layout.addWidget(self.plot_widget)

                # initial plot
                self.update_plot()

                # timer for live updates
                self.timer = QTimer(self)
                self.timer.timeout.connect(self.update_plot)
                self.timer.start(self.refresh_interval * 1000)
            except Exception:
                self.layout.addWidget(QLabel("Live chart not available (plot init failed)."))
        else:
            self.layout.addWidget(QLabel("Live chart not available (missing libs)."))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        self.layout.addWidget(buttons)
        self.setLayout(self.layout)

    def update_plot(self):
        try:
            # Try intraday 1m first, fallback to daily
            data = None
            try:
                data = yf.Ticker(self.symbol).history(period="7d", interval="1m")
            except Exception:
                data = yf.Ticker(self.symbol).history(period="3mo", interval="1d")

            if data is None or data.empty or self.plot_widget is None:
                return

            x = data.index.astype('datetime64[ms]').astype('int64')
            y = data["Close"].values

            # pyqtgraph prefers numeric x; convert to timestamps
            self.plot_curve.setData(x, y)
            # set axis range
            self.plot_widget.setXRange(x[0], x[-1], padding=0.01)
            ymin, ymax = float(min(y)), float(max(y))
            self.plot_widget.setYRange(ymin * 0.995, ymax * 1.005, padding=0)
        except Exception:
            pass



class ScannerSignals(QObject):
    """Signals for thread-safe GUI updates."""
    state_updated = pyqtSignal(dict)
    regime_updated = pyqtSignal(dict)


class ScannerThread(threading.Thread):
    """Background thread for scanner updates."""
    def __init__(self, signals):
        super().__init__(daemon=True)
        self.signals = signals
        self.running = True
        
    def run(self):
        # Start background scanner
        try:
            start_background_scanner_thread()
        except Exception as e:
            print(f"Scanner thread error: {e}")


class APEXScannerGUI(QMainWindow):
    """Main desktop application window."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("APEX Scanner")
        self.setGeometry(100, 100, 1400, 800)
        self.signals = ScannerSignals()
        self.signals.state_updated.connect(self.update_results)
        self.signals.regime_updated.connect(self.update_regime)
        
        # Start background scanner
        scanner_thread = ScannerThread(self.signals)
        scanner_thread.start()

        self.latest_state = {}
        self.init_ui()
        
        # Update timer (refresh every 5 seconds)
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh_data)
        self.timer.start(5000)
        
    def init_ui(self):
        """Initialize the GUI."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout()
        
        # Header with title
        header_layout = QHBoxLayout()
        title = QLabel("APEX Stock Scanner")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title.setFont(title_font)
        header_layout.addWidget(title)
        header_layout.addStretch()

        # Auto-refresh toggle
        self.auto_refresh_cb = QCheckBox("Auto-refresh")
        self.auto_refresh_cb.setChecked(True)
        self.auto_refresh_cb.stateChanged.connect(self.on_auto_refresh_toggled)
        header_layout.addWidget(self.auto_refresh_cb)

        # Session selector
        session_label = QLabel("Session:")
        self.session_combo = QComboBox()
        self.session_combo.addItems(["regular", "pre-market", "after-hours"])
        self.session_combo.currentTextChanged.connect(self.on_session_changed)
        header_layout.addWidget(session_label)
        header_layout.addWidget(self.session_combo)

        # Refresh button
        refresh_btn = QPushButton("Refresh Now")
        refresh_btn.clicked.connect(self.on_refresh)
        header_layout.addWidget(refresh_btn)

        # Open in browser (optional)
        open_btn = QPushButton("Open Web Dashboard")
        open_btn.clicked.connect(self.open_web_dashboard)
        header_layout.addWidget(open_btn)

        layout.addLayout(header_layout)
        
        # Tabs
        tabs = QTabWidget()
        
        # Dashboard tab
        dashboard_widget = self.create_dashboard_tab()
        tabs.addTab(dashboard_widget, "Dashboard")
        
        layout.addWidget(tabs)
        
        # Status bar
        self.statusBar().showMessage("Initializing scanner...")
        
        central_widget.setLayout(layout)
        
    def create_dashboard_tab(self):
        """Create the main dashboard tab."""
        widget = QWidget()
        layout = QVBoxLayout()
        
        # Top summary cards (Regime / SPY / VIX / Top sector)
        cards_layout = QHBoxLayout()
        self.card_regime = QLabel("Regime: Loading...")
        self.card_spy = QLabel("SPY: --")
        self.card_vix = QLabel("VIX: --")
        self.card_sector = QLabel("Top Sector: --")
        for c in (self.card_regime, self.card_spy, self.card_vix, self.card_sector):
            c.setStyleSheet("background:#1f1f1f; color: #f0f0f0; padding:10px; border-radius:6px;")
            c.setFixedHeight(48)
            cards_layout.addWidget(c)
        cards_layout.addStretch()
        layout.addLayout(cards_layout)
        
        # Results table
        self.table = QTableWidget()
        self.table.setColumnCount(15)
        self.table.setHorizontalHeaderLabels([
            "#", "Symbol", "Dir", "Score", "Conf", "Price", "Entry", 
            "Stop", "TP1", "TP2", "R/R", "RVol", "Shares", "Risk$", "Flags"
        ])
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(14, 200)
        
        layout.addWidget(QLabel("Top Setups:"))
        layout.addWidget(self.table)
        
        widget.setLayout(layout)
        return widget
        
    def refresh_data(self):
        """Refresh scanner data from background process."""
        try:
            session = self.session_combo.currentText()
            state = get_cached_state(session)
            self.latest_state = state
            self.signals.state_updated.emit(state)
            
            regime = get_market_regime()
            regime_dict = {
                "classification": regime.classification,
                "spy_price": regime.spy_price,
                "spy_vs_50sma": regime.spy_vs_50sma,
                "vix": regime.vix,
            }
            self.signals.regime_updated.emit(regime_dict)
        except Exception as e:
            self.statusBar().showMessage(f"Error: {str(e)}")
            
    def update_results(self, state):
        """Update the results table."""
        results = state.get("results", []) or []
        self.table.setRowCount(len(results))
        
        for idx, row in enumerate(results):
            self.table.setItem(idx, 0, QTableWidgetItem(str(idx + 1)))
            self.table.setItem(idx, 1, QTableWidgetItem(row.get("symbol", "")))
            self.table.setItem(idx, 2, QTableWidgetItem(row.get("signal_direction", "")))
            self.table.setItem(idx, 3, QTableWidgetItem(f"{row.get('composite', 0):.2f}"))
            self.table.setItem(idx, 4, QTableWidgetItem(str(row.get("confidence", ""))))
            self.table.setItem(idx, 5, QTableWidgetItem(f"{row.get('price', 0):.2f}"))
            self.table.setItem(idx, 6, QTableWidgetItem(f"{row.get('entry', 0):.2f}"))
            self.table.setItem(idx, 7, QTableWidgetItem(f"{row.get('stop_loss', 0):.2f}"))
            self.table.setItem(idx, 8, QTableWidgetItem(f"{row.get('tp1', 0):.2f}"))
            self.table.setItem(idx, 9, QTableWidgetItem(f"{row.get('tp2', 0):.2f}"))
            self.table.setItem(idx, 10, QTableWidgetItem(f"{row.get('rr_ratio', 0):.2f}"))
            self.table.setItem(idx, 11, QTableWidgetItem(f"{row.get('rel_volume', 0):.2f}"))
            self.table.setItem(idx, 12, QTableWidgetItem(str(row.get("plan_shares", ""))))
            self.table.setItem(idx, 13, QTableWidgetItem(f"{row.get('risk_dollars', 0):.2f}"))
            self.table.setItem(idx, 14, QTableWidgetItem(", ".join(row.get("flags", []))))

        last_scan = state.get("last_scan", "Never")
        status_msg = f"Last scan: {last_scan} | Showing {len(results)} setups"
        self.statusBar().showMessage(status_msg)
        
        # enable double-click to open details
        try:
            self.table.cellDoubleClicked.disconnect()
        except Exception:
            pass
        self.table.cellDoubleClicked.connect(self.on_cell_double_clicked)

    def update_regime(self, regime):
        """Update the market regime display."""
        self.card_regime.setText(f"Regime: {regime['classification']}")
        self.card_spy.setText(f"SPY: ${regime['spy_price']:.2f}")
        self.card_vix.setText(f"VIX: {regime['vix']:.1f}")
        # top sector may be missing from regime object; keep generic
        self.card_sector.setText(f"Top Sector: {regime.get('top_sector','N/A')}")
        
    def on_session_changed(self, session):
        """Handle session selection change."""
        try:
            set_forced_session(session)
            self.refresh_data()
        except Exception as e:
            self.statusBar().showMessage(f"Error changing session: {e}")
            
    def on_refresh(self):
        """Handle manual refresh button."""
        self.statusBar().showMessage("Refreshing...")
        self.refresh_data()

    def on_auto_refresh_toggled(self, state):
        if state == Qt.Checked:
            self.timer.start(5000)
            self.statusBar().showMessage("Auto-refresh enabled")
        else:
            self.timer.stop()
            self.statusBar().showMessage("Auto-refresh disabled")

    def open_web_dashboard(self):
        import webbrowser
        # assume Flask would run on first free port
        url = f"http://localhost:8000"
        webbrowser.open(url)

    def on_cell_double_clicked(self, row, col):
        try:
            symbol_item = self.table.item(row, 1)
            if not symbol_item:
                return
            symbol = symbol_item.text()
            # find details in latest_state
            results = (self.latest_state or {}).get("results") or []
            details = None
            for r in results:
                if r.get("symbol") == symbol:
                    details = r
                    break
            if details is None:
                details = {"symbol": symbol, "info": "Not found in cache"}

            dlg = DetailsDialog(self, symbol, details, refresh_interval=30)
            dlg.exec_()
        except Exception as e:
            self.statusBar().showMessage(f"Error showing details: {e}")


def main():
    app = QApplication(sys.argv)
    window = APEXScannerGUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
