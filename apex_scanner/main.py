"""
main.py  –  CLI Entry Point
=============================
Usage:
  python main.py scan              # single scan → print table
  python main.py scan --loop       # continuous scan every N minutes
  python main.py scan --refresh    # force fresh universe (bypass cache)
  python main.py backtest          # replay stored DB signals
  python main.py performance       # print lifetime P&L stats
  python main.py regime            # print market regime only

Environment:
  Copy .env.example → .env and fill in any optional API keys/webhooks.
  All data sources used are FREE (yfinance + Yahoo Finance + NASDAQ FTP).
"""

import sys
import argparse
import logging
import socket
import warnings
from pathlib import Path

# Add project root to path so scanner package imports work
sys.path.insert(0, str(Path(__file__).parent))


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

warnings.filterwarnings(
    "ignore",
    category=Warning,
    module=r"urllib3.*",
)
warnings.filterwarnings(
    "ignore",
    category=Warning,
    module=r"requests\.packages\.urllib3.*",
)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)

# Import scanner utilities but tolerate missing optional deps by falling
# back to simple logger/console implementations. This keeps the CLI usable
# for lightweight operations even when full numeric/analysis libs aren't
# installed.
try:
    from scanner.utils import setup_logging, logger, console
except Exception:
    import logging, sys

    logger = logging.getLogger("apex")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    class _SimpleConsole:
        def print(self, *args, **kwargs):
            sep = kwargs.get("sep", " ")
            end = kwargs.get("end", "\n")
            file = kwargs.get("file", sys.stdout)
            file.write(sep.join(str(a) for a in args) + end)

    console = _SimpleConsole()
    def setup_logging(*a, **k):
        return logger

# Delay importing heavy modules (requests, yfinance, pandas, etc.) until
# they're actually needed. Importing `scanner.background` or `web` at module
# level pulled in those dependencies and caused the CLI to crash when some
# optional packages weren't installed.
try:
    from scanner.config import CONFIG
except Exception:
    # Provide minimal fallback defaults so the CLI can still show a banner
    # and run lightweight commands even when full scanner dependencies
    # (or dotenv) aren't available.
    class _DefaultConfig:
        ACCOUNT_SIZE = 130.0
        MAX_RISK_PER_TRADE_PCT = 0.05
        MIN_RISK_REWARD = 1.2
        SCAN_INTERVAL_MIN = 7
        TOP_PICKS = 12
        MIN_SCORE_TO_ALERT = 65.0
        # reasonable placeholders for other code paths that may inspect CONFIG
        MAX_STAGE3_PASS = 20
        MAX_STAGE3_PASS_EXTENDED = 20
        TOP_PICKS_EXTENDED = 10
        CACHE_TTL_SECS = 300

    CONFIG = _DefaultConfig()


def _print_cached_results(state: dict) -> None:
    from rich.table import Table
    from rich.console import Console
    from rich import box

    console = Console()
    results = state.get("results", []) or []
    regime = state.get("regime") or {}
    console.print(f"\n[bold cyan]Market Regime:[/bold cyan] [bold]{regime.get('classification', 'Unknown')}[/bold]  "
                  f"SPY vs 50SMA={regime.get('spy_vs_50sma', 0):+.1f}%  "
                  f"Top sector: {regime.get('top_sector', 'N/A')}\n")

    tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan",
                title=f"Cached Top {len(results)} Setups", border_style="dim")
    tbl.add_column("#", style="dim", width=3)
    tbl.add_column("Symbol", style="bold white", width=7)
    tbl.add_column("Dir", width=6)
    tbl.add_column("Score", width=7)
    tbl.add_column("Conf", width=10)
    tbl.add_column("Price", width=9)
    tbl.add_column("Entry", width=9)
    tbl.add_column("Stop", width=9)
    tbl.add_column("TP1", width=9)
    tbl.add_column("TP2", width=9)
    tbl.add_column("R/R", width=5)
    tbl.add_column("RVol", width=6)
    tbl.add_column("Shares", width=7)
    tbl.add_column("Risk$", width=7)
    tbl.add_column("Flags", width=30)

    for index, row in enumerate(results, start=1):
        tbl.add_row(
            str(index),
            row.get("symbol", ""),
            row.get("signal_direction", ""),
            f"{row.get('composite', 0):.2f}",
            str(row.get("confidence", "")),
            f"{row.get('price', 0):.2f}",
            f"{row.get('entry', 0):.2f}",
            f"{row.get('stop_loss', 0):.2f}",
            f"{row.get('tp1', 0):.2f}",
            f"{row.get('tp2', 0):.2f}",
            f"{row.get('rr_ratio', 0):.2f}",
            f"{row.get('rel_volume', 0):.2f}",
            f"{row.get('plan_shares', '')}",
            f"{row.get('risk_dollars', 0):.2f}",
            ", ".join(row.get("flags", [])) if row.get("flags") else "",
        )

    console.print(tbl)
    console.print(f"\n[dim]Cached state: {state.get('status', 'Unknown')} | "
                  f"Last scan: {state.get('last_scan')} | "
                  f"Session: {state.get('session')}[/dim]\n")


def cmd_scan(args) -> None:
    # Import scanner submodules lazily to avoid pulling heavy optional
    # dependencies during simple CLI usage (like `dashboard`).
    if args.loop or args.refresh:
        from scanner import run_full_scan, continuous_scan, _print_table

    if args.loop:
        logger.info(f"Starting continuous scan every {CONFIG.SCAN_INTERVAL_MIN} minutes …")
        continuous_scan(interval_min=CONFIG.SCAN_INTERVAL_MIN)
        return

    if args.refresh:
        results, regime, _stats = run_full_scan(
            force_universe_refresh=True,
            market_session=args.session,
        )
        _print_table(results, regime)
        if not results:
            console.print("[yellow]No valid setups found this scan – try again in 15 min.[/yellow]")
        return

    from scanner.background import get_cached_state
    state = get_cached_state(args.session)
    if not state.get("results"):
        console.print("[yellow]No cached scan results available yet. Waiting for the background scanner to complete.[/yellow]")
        console.print(f"[dim]Status: {state.get('status')} | Last scan: {state.get('last_scan')}[/dim]")
        return

    console.print("[dim]Showing cached background scan results. Use --refresh to force a fresh manual scan.[/dim]\n")
    _print_cached_results(state)


def cmd_backtest(_args) -> None:
    from scanner.backtest import backtest_from_db
    report = backtest_from_db(days=30)
    if not report.trades:
        console.print("[yellow]No stored signals found. Run a scan first to accumulate signals.[/yellow]")


def cmd_performance(_args) -> None:
    from scanner.database import DB
    stats = DB.lifetime_stats()
    perf  = DB.get_performance_summary(days=30)

    console.print("\n[bold cyan]──── Lifetime Stats ────[/bold cyan]")
    console.print(f"  Total Trades  : {stats.get('trades', 0)}")
    console.print(f"  Win Rate      : {stats.get('win_rate', 0)*100:.1f}%")
    console.print(f"  Total P&L     : ${stats.get('total_pnl', 0):,.2f}")
    console.print(f"  Avg Win       : ${stats.get('avg_win', 0):,.2f}")
    console.print(f"  Avg Loss      : ${stats.get('avg_loss', 0):,.2f}")

    if perf:
        console.print("\n[bold cyan]──── Last 30 Days ────[/bold cyan]")
        for row in perf[:10]:
            pnl_str = f"+${row['net_pnl']:,.2f}" if row['net_pnl'] >= 0 else f"-${abs(row['net_pnl']):,.2f}"
            console.print(f"  {row['date']}  Trades={row['trades']:3d}  "
                          f"WR={row['win_rate']*100:.0f}%  PnL={pnl_str}")


def cmd_regime(_args) -> None:
    from scanner.market_regime import get_market_regime
    r = get_market_regime()
    console.print(f"\n[bold cyan]Market Regime[/bold cyan]: [bold]{r.classification}[/bold]")
    console.print(f"  SPY price     : ${r.spy_price:.2f}")
    console.print(f"  SPY vs 50 SMA : {r.spy_vs_50sma:+.2f}%")
    console.print(f"  SPY vs 200 SMA: {r.spy_vs_200sma:+.2f}%")
    console.print(f"  VIX           : {r.vix:.1f}  [{r.vix_regime}]")
    console.print(f"  Breadth proxy : {r.breadth*100:.0f}% above 50 SMA")
    console.print(f"  Top sector    : {r.top_sector}")
    console.print(f"  Weak sector   : {r.bot_sector}")
    if r.sector_scores:
        console.print("\n[dim]  Sector 1-month returns:[/dim]")
        for etf, chg in sorted(r.sector_scores.items(), key=lambda x: x[1], reverse=True):
            bar = "█" * int(abs(chg) / 2)
            sign = "+" if chg >= 0 else ""
            console.print(f"    {etf:<6}  {sign}{chg:.1f}%  {bar}")


def cmd_dashboard(args) -> None:
    # Start a background scanner unless explicitly disabled.
    if not getattr(args, "no_scanner", False):
        # init_scanner_daemon may import heavy scanner modules; import here.
        try:
            from .web import init_scanner_daemon
        except Exception as e:
            console.print(f"[red]Web dependencies are not installed: {e}[/red]")
            console.print("Install Flask and related packages to run the dashboard: pip install -r requirements-dev.txt")
            return
        use_process = not getattr(args, "foreground", False)
        init_scanner_daemon(use_process=use_process)
        console.print(f"[dim]  Background scanner process is running in parallel.[/dim]\n")
    else:
        console.print(f"[dim]  Dashboard starting without a local scanner. Use a separate scanner service or refresh cache manually.[/dim]\n")

    # Import the web app (Flask) lazily so missing web deps don't break other CLI commands.
    try:
        from .web import app as web_app
    except Exception as e:
        console.print(f"[red]Web dependencies are not installed: {e}[/red]")
        console.print("Install Flask and related packages to run the dashboard: pip install -r requirements-dev.txt")
        return
    port = args.port if getattr(args, "port", None) else find_free_port(8000)
    console.print(f"[dim]  Web dashboard starting on http://localhost:{port}[/dim]\n")
    web_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ── Entry ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="apex-scanner",
        description="APEX Stock Scanner – dynamic, pipeline-based momentum & breakout scanner",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    # scan
    p_scan = sub.add_parser("scan", help="Run the scanner")
    p_scan.add_argument("--loop",    action="store_true",
                        help="Continuous scan every SCAN_INTERVAL_MINUTES")
    p_scan.add_argument("--refresh", action="store_true",
                        help="Force fresh universe list (bypass cache)")
    p_scan.add_argument("--session", choices=["regular", "pre-market", "after-hours"], default="regular",
                        help="Choose the market session for this scan")
    p_scan.add_argument("--verbose", action="store_true",
                        help="Enable DEBUG logging")
    p_scan.set_defaults(func=cmd_scan)

    # backtest
    p_bt = sub.add_parser("backtest", help="Replay stored signals against real data")
    p_bt.set_defaults(func=cmd_backtest)

    # performance
    p_perf = sub.add_parser("performance", help="Print trading performance stats")
    p_perf.set_defaults(func=cmd_performance)

    # regime
    p_reg = sub.add_parser("regime", help="Print current market regime")
    p_reg.set_defaults(func=cmd_regime)

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Run the web dashboard")
    p_dash.add_argument("--port", type=int, default=None,
                        help="Dashboard port (default: first free port starting at 8000)")
    p_dash.add_argument("--foreground", action="store_true",
                        help="Run background scanner in-process (thread) so logs are visible in this terminal")
    p_dash.add_argument("--no-scanner", action="store_true",
                        help="Start the web dashboard without launching a background scanner")
    p_dash.set_defaults(func=cmd_dashboard)

    args = parser.parse_args()
    if args.command is None:
        args = parser.parse_args(["dashboard"])

    # Logging level
    if getattr(args, "verbose", False):
        logging.getLogger().setLevel(logging.DEBUG)


    # Banner
    console.print("\n[bold cyan]"
                  " ██████╗  █████╗ ██████╗ ██╗  ██╗\n"
                  " ██╔══██╗██╔══██╗██╔══██╗╚██╗██╔╝\n"
                  " ███████║███████║███████║ ╚███╔╝ \n"
                  " ██╔══██║██╔══██║██╔═══╝ ██╔██╗ \n"
                  " ██║  ██║██║  ██║██║     ██╔╝ ██╗\n"
                  " ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝[/bold cyan]")
    console.print("[dim]  Dynamic Stock Scanner  –  Free APIs only[/dim]\n")
    console.print(f"[dim]  Account: ${CONFIG.ACCOUNT_SIZE:,.0f}  |  "
                  f"Risk/trade: {CONFIG.MAX_RISK_PER_TRADE_PCT*100:.1f}%  |  "
                  f"Min R/R: {CONFIG.MIN_RISK_REWARD}×[/dim]\n")

    args.func(args)


if __name__ == "__main__":
    main()
 