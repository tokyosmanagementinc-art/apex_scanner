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
    QTabWidget, QStatusBar, QScrollArea
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QFont, QColor

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.background import get_cached_state, start_background_scanner_thread, set_forced_session
from scanner.market_regime import get_market_regime
from scanner.utils import fmt_price
from scanner.config import CONFIG


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
        
        # Header with title and controls
        header_layout = QHBoxLayout()
        title = QLabel("APEX Stock Scanner")
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        header_layout.addWidget(title)
        header_layout.addStretch()
        
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
        
        # Market regime section
        regime_layout = QHBoxLayout()
        regime_label = QLabel("Market Regime:")
        regime_font = QFont()
        regime_font.setBold(True)
        regime_label.setFont(regime_font)
        self.regime_display = QLabel("Loading...")
        regime_layout.addWidget(regime_label)
        regime_layout.addWidget(self.regime_display)
        regime_layout.addStretch()
        layout.addLayout(regime_layout)
        
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
        
    def update_regime(self, regime):
        """Update the market regime display."""
        text = f"{regime['classification']} | SPY: ${regime['spy_price']:.2f} | vs 50SMA: {regime['spy_vs_50sma']:+.2f}% | VIX: {regime['vix']:.1f}"
        self.regime_display.setText(text)
        
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


def main():
    app = QApplication(sys.argv)
    window = APEXScannerGUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
