# Hosting: tidal-server on electro

This walks through deploying `tidal-server` alongside the existing services on electro.

- **Subdomain**: `api.tidal.wavey.info`
- **Port**: 8020 (localhost only, nginx terminates SSL)
- **User**: `wavey`
- **Working directory**: `/home/wavey/tidal`

## 1. DNS

Add an A record before anything else (certbot needs it to resolve):

```
api.tidal.wavey.info  A  95.217.202.159
```

## 2. Environment

Create `/home/wavey/tidal/.env`:

```bash
# Database
TIDAL_DB_PATH=data/tidal.db

# RPC (local geth)
RPC_URL=http://127.0.0.1:8545

# API server
TIDAL_API_PORT=8020
TIDAL_API_HOST=127.0.0.1

# Scan/kick daemon config (uncomment when ready)
# TIDAL_SCAN_INTERVAL=300
# TIDAL_KICK_INTERVAL=60
```

## 3. Install and Migrate

```bash
cd /home/wavey/tidal
uv venv venv --python 3.12
source venv/bin/activate
uv pip install -e .

# Verify entrypoints
tidal-server --help

# Initialize the database
mkdir -p data
tidal-server db migrate

# Create an API key for each operator
tidal-server auth create --label wavey
# Store the printed key — it cannot be retrieved again
```

## 4. Systemd

### API server

`sudo nano /etc/systemd/system/tidal-server.service`:

```ini
[Unit]
Description=Tidal API Server (FastAPI/uvicorn)
After=network.target

[Service]
Type=simple
User=wavey
Group=wavey
WorkingDirectory=/home/wavey/tidal
EnvironmentFile=/home/wavey/tidal/.env
ExecStart=/home/wavey/tidal/venv/bin/tidal-server api serve --host 127.0.0.1 --port 8020
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Scan daemon (optional, enable when ready)

`sudo nano /etc/systemd/system/tidal-scan-daemon.service`:

```ini
[Unit]
Description=Tidal Scan Daemon
After=tidal-server.service

[Service]
Type=simple
User=wavey
Group=wavey
WorkingDirectory=/home/wavey/tidal
EnvironmentFile=/home/wavey/tidal/.env
ExecStart=/home/wavey/tidal/venv/bin/tidal-server scan daemon
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Kick daemon (optional, enable when ready)

`sudo nano /etc/systemd/system/tidal-kick-daemon.service`:

```ini
[Unit]
Description=Tidal Kick Daemon
After=tidal-server.service

[Service]
Type=simple
User=wavey
Group=wavey
WorkingDirectory=/home/wavey/tidal
EnvironmentFile=/home/wavey/tidal/.env
ExecStart=/home/wavey/tidal/venv/bin/tidal-server kick daemon
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Start it up

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tidal-server

# Verify it's running
curl http://127.0.0.1:8020/api/v1/tidal/health
```

## 5. Nginx

`sudo nano /etc/nginx/sites-available/api.tidal.wavey.info`:

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

    ssl_certificate     /etc/letsencrypt/live/api.tidal.wavey.info/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.tidal.wavey.info/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    access_log /var/log/nginx/api.tidal.wavey.info.access.log;
    error_log  /var/log/nginx/api.tidal.wavey.info.error.log;

    location / {
        proxy_pass http://127.0.0.1:8020;
        proxy_http_version 1.1;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 5s;
        proxy_send_timeout    60s;
        proxy_read_timeout    60s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/api.tidal.wavey.info /etc/nginx/sites-enabled/
sudo nginx -t
sudo certbot --nginx -d api.tidal.wavey.info
sudo systemctl reload nginx
```

## 6. Verify

```bash
# Through nginx
curl https://api.tidal.wavey.info/api/v1/tidal/health

# From a laptop (operator CLI, using the key from step 3)
tidal --api-base-url https://api.tidal.wavey.info --api-token <key> logs kicks
```

## Notes

**SQLite and WAL mode**: The API server, scan daemon, and kick daemon all share the same SQLite file under `data/tidal.db`. WAL mode handles concurrent readers with a single writer, which is fine for this workload. All three run as `wavey` from the same working directory, so file permissions are not an issue.

**Gitignore**: Add `data/` to `.gitignore` so the database file is never committed.

**Updating**: After pulling new code, reinstall and restart:

```bash
cd /home/wavey/tidal
git pull
uv pip install -e .
tidal-server db migrate
sudo systemctl restart tidal-server
```

**Logs**:

```bash
journalctl -u tidal-server -f
journalctl -u tidal-scan-daemon -f
journalctl -u tidal-kick-daemon -f
```
