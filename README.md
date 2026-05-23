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
- The scanner now uses penny-stock-focused filters by default: low price, higher minimum volume, stronger rel-vol, and positive gap activity.
- The default dashboard server is Flask and runs on `0.0.0.0`.
- `--foreground` is useful for debugging since it runs the scanner thread in the same process.

## Docker

Build the image:

```bash
docker build -t apex_scanner .
```

Run the dashboard container:

```bash
docker run --rm -p 8000:8000 -v "$PWD/cache":/app/cache -v "$PWD/logs":/app/logs -v "$PWD/data":/app/data apex_scanner
```

Run both the web dashboard and scanner in Docker Compose:

```bash
docker compose up --build
```

The compose setup uses a shared `cache`, `logs`, and `data` volume so the scanner service can write state and the web service can read it.

## Production deployment

A production deployment can use `docker-compose.prod.yml` with restart policies and detached mode. The separate `web` and `scanner` services share `cache`, `logs`, and `data` directories for live state sync.

For a systemd-managed deployment, use `deploy/apex_scanner.service` and update the `WorkingDirectory` path to your cloned repo.

Copy the example environment file before running:

```bash
cp scanner/.env.example scanner/.env
```

Then configure any API keys or settings in `scanner/.env`.

## Tests

Install dev dependencies and run the test suite:

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

The project also includes a GitHub Actions workflow at `.github/workflows/ci.yml` that installs dependencies and runs the tests on push.
