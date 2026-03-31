# Server Operator Guide

Use this page after [Install](install.md). It focuses on operating the shared Tidal host.

## What The Server Operator Owns

`tidal-server` is the server operator CLI. It owns:

- Alembic migrations
- scanner execution
- optional kick daemon execution
- FastAPI serving
- API key management
- the canonical SQLite database

The server should run close to both the database and the Ethereum RPC it depends on.

## Recommended Deployment Shape

Minimal production deployment:

```text
1 host / VM
  - SQLite database file
  - tidal-server scan daemon
  - tidal-server api serve
  - optional tidal-server kick daemon
```

Separate the CLI client wallet from this machine whenever possible.

## First-Time Bootstrap

After following [Install](install.md), review:

- `config/server.yaml`
- `config/.env` for local repo use, or `TIDAL_ENV_FILE` for a secret path outside Git
- `TIDAL_HOME` if you want state outside the repo checkout

Then run:

```bash
tidal-server db migrate --config config/server.yaml
tidal-server auth create --label cli-client-name --config config/server.yaml
tidal-server scan run --config config/server.yaml
tidal-server api serve --config config/server.yaml
```

If you plan to reconcile receipts in the API process, set `RPC_URL` so the background reconciler can start.

## Example Linux Deployment

One simple production shape is:

- host: `electro`
- user: `wavey`
- working directory: `/home/wavey/tidal`
- API bind: `127.0.0.1:8020`
- reverse proxy: nginx terminating TLS at `api.tidal.wavey.info`

Example `config/.env`:

```bash
RPC_URL=http://127.0.0.1:8545
TOKEN_PRICE_AGG_KEY=...
```

Example `config/server.yaml` overrides:

```yaml
tidal_api_host: 127.0.0.1
tidal_api_port: 8020
scan_interval_seconds: 300
scan_auto_settle_enabled: false
```

## Long-Running Commands

Scanner daemon:

```bash
tidal-server scan daemon --config config/server.yaml --interval-seconds 300
```

Kick daemon:

```bash
tidal-server kick daemon --config config/server.yaml --broadcast --sender 0xYourAddress --account wavey3
```

API:

```bash
tidal-server api serve --config config/server.yaml
```

The API host and port normally come from `config/server.yaml`:

- `tidal_api_host`
- `tidal_api_port`

## systemd Example

API service:

```ini
[Unit]
Description=Tidal API Server (FastAPI/uvicorn)
After=network.target

[Service]
Type=simple
User=wavey
Group=wavey
WorkingDirectory=/home/wavey/tidal
Environment=TIDAL_HOME=/var/lib/tidal
EnvironmentFile=/home/wavey/tidal/config/.env
ExecStart=/home/wavey/.local/bin/tidal-server api serve --config /home/wavey/tidal/config/server.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Scanner oneshot service:

```ini
[Unit]
Description=Tidal Scan (oneshot)
After=network.target

[Service]
Type=oneshot
User=wavey
Group=wavey
WorkingDirectory=/home/wavey/tidal
Environment=TIDAL_HOME=/var/lib/tidal
EnvironmentFile=/home/wavey/tidal/config/.env
ExecStart=/home/wavey/.local/bin/tidal-server scan run --config /home/wavey/tidal/config/server.yaml
```

Pair the scan oneshot with a systemd timer or external scheduler.

Adjust `/home/wavey/.local/bin/tidal-server` to whatever `command -v tidal-server` returns on the target host.

## Reverse Proxy Example

Minimal nginx shape:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name api.tidal.wavey.info;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name api.tidal.wavey.info;

    location / {
        proxy_pass http://127.0.0.1:8020;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## DNS And TLS

For the API hostname, point an `A` record at your server before requesting certificates:

```text
api.tidal.wavey.info A <server-ip>
```

Then issue the certificate with your normal ACME flow, for example `certbot --nginx`.

## API Key Management

Create:

```bash
tidal-server auth create --label alice --config config/server.yaml
```

List:

```bash
tidal-server auth list --config config/server.yaml
```

Revoke:

```bash
tidal-server auth revoke alice --config config/server.yaml
```

The API stores only SHA-256 hashes of keys. The plaintext key is shown once at creation time.

## Database Notes

SQLite is the canonical datastore for this repo.

Runtime behavior:

- journal mode: WAL
- busy timeout: 30 seconds
- synchronous mode: NORMAL

That configuration is set in `tidal/persistence/db.py`.

Operational implications:

- keep the database on local disk
- avoid multiple independent writers outside the app
- expect occasional lock retries under write pressure
- back up the `.db`, `.db-wal`, and `.db-shm` files consistently

Also ignore runtime data directories in git. A server-local `data/` directory should never be committed.
With the current layout, the canonical runtime home is `~/.tidal/`, not a repo-local `data/` directory.

## Auth Model

Public endpoints:

- dashboard
- logs
- kick inspect
- deploy defaults
- AuctionScan lookups
- health

Authenticated endpoints:

- kick prepare
- auction prepare routes
- action audit routes

Authentication is bearer-token based. Operator identity is currently the API key label.

## Monitoring And Troubleshooting

Useful commands:

```bash
tidal-server logs scans
tidal-server logs kicks
tidal-server logs show <run_id>
```

## Command Reference

Use these pages for the exact server operator command surfaces:

- [CLI Command Map](cli-reference.md)
- [Server Operator: `tidal-server db`](cli-server-db.md)
- [Server Operator: `tidal-server scan`](cli-server-scan.md)
- [Server Operator: `tidal-server api`](cli-server-api.md)
- [Server Operator: `tidal-server auth`](cli-server-auth.md)
- [Server Operator: `tidal-server kick`](cli-server-kick.md)
- [Server Operator: `tidal-server auction`](cli-server-auction.md)
- [Server Operator: `tidal-server logs`](cli-server-logs.md)

Useful symptoms:

- no candidates: check scanner freshness, token prices, and auction mappings
- repeated `database is locked`: investigate overlapping long-lived writes
- API 503 `No API keys configured`: create at least one key with `tidal-server auth create`
- missing receipt reconciliation: verify `RPC_URL` is present in the API process environment

Useful logs:

```bash
journalctl -u tidal-api -f
journalctl -u tidal-scan -f
```

## Deployment Boundaries

Do not point multiple CLI clients directly at the SQLite database. The intended model is:

- server owns DB and preparation logic
- CLI client talks over HTTP
- CLI client signs locally

That keeps schema changes and audit behavior centralized.
