# Deployment

This repo supports production deployment via Docker Compose and optional systemd.

## Production Docker Compose

Use `docker-compose.prod.yml` for a production-ready deployment setup.

1. Build the image:

```bash
docker compose -f docker-compose.prod.yml build
```

2. Start services:

```bash
docker compose -f docker-compose.prod.yml up -d
```

3. Stop services:

```bash
docker compose -f docker-compose.prod.yml down
```

The production compose file runs two services:

- `web`: Flask dashboard only, pointing at shared local `cache`, `logs`, and `data`
- `scanner`: background scanner, writing state into the shared cache

## Systemd service

If you want systemd to manage the Compose stack, copy `deploy/apex_scanner.service` to `/etc/systemd/system/apex_scanner.service` and update the `WorkingDirectory` and file paths.

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable apex_scanner.service
sudo systemctl start apex_scanner.service
```

## Environment

Copy the example environment file:

```bash
cp scanner/.env.example scanner/.env
```

Then update `scanner/.env` with any API keys or optional settings before starting.
