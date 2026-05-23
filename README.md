# apex_scanner

A lightweight stock scanner that runs an automated market scan and serves a live dashboard via Flask.

## Quick start

```bash
python main.py dashboard
```

This starts the web dashboard on the first free port beginning at `8000`.

## Commands

### Dashboard

```bash
python main.py dashboard
```

Open the dashboard in your browser at the printed URL.

### Dashboard with terminal-visible scan logs

```bash
python main.py dashboard --foreground
```

This starts the scanner in-process and keeps scanner logs visible in the same terminal.

### Manual scan

```bash
python main.py scan
```

If cached results are available, this prints the last background scan.

### Force a fresh scan

```bash
python main.py scan --refresh
```

This bypasses the universe cache and performs a fresh scan immediately.

### Continuous scan

```bash
python main.py scan --loop
```

This keeps scanning every configured interval.

### Other utilities

```bash
python main.py regime
python main.py performance
python main.py backtest
```

## Notes

- The dashboard uses cached scan state from the background scanner.
- The default dashboard server is Flask and runs on `0.0.0.0`.
- `--foreground` is useful for debugging since it runs the scanner thread in the same process.
