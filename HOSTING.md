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
DB_PATH=data/tidal.db

# RPC (local geth)
RPC_URL=http://127.0.0.1:8545

# API server
TIDAL_API_PORT=8020
TIDAL_API_HOST=127.0.0.1
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

`sudo nano /etc/systemd/system/tidal-api.service`:

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
ExecStart=/home/wavey/tidal/venv/bin/tidal-server api serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Scan (oneshot, triggered by timer)

`sudo nano /etc/systemd/system/tidal-scan.service`:

```ini
[Unit]
Description=Tidal Scan (oneshot)
After=network.target

[Service]
Type=oneshot
User=wavey
Group=wavey
WorkingDirectory=/home/wavey/tidal
EnvironmentFile=/home/wavey/tidal/.env
ExecStart=/home/wavey/tidal/venv/bin/tidal-server scan run
```

Pair this with your existing systemd timer that triggers `tidal-scan.service`.

### Start it up

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tidal-api

# Verify it's running
curl http://127.0.0.1:8020/health
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
curl https://api.tidal.wavey.info/health

# From a laptop (operator CLI, using the key from step 3)
export TIDAL_API_BASE_URL=https://api.tidal.wavey.info
export TIDAL_API_KEY=<key>
tidal logs kicks
```

## Notes

**SQLite and WAL mode**: The API server and scan service share the same SQLite file at `data/tidal.db`. WAL mode handles concurrent readers with a single writer, which is fine for this workload. Both run as `wavey` from the same working directory, so file permissions are not an issue.

**Gitignore**: Add `data/` to `.gitignore` so the database file is never committed.

**Updating**: After pulling new code, reinstall and restart:

```bash
cd /home/wavey/tidal
git pull
uv pip install -e .
tidal-server db migrate
sudo systemctl restart tidal-api
```

**Logs**:

```bash
journalctl -u tidal-api -f
journalctl -u tidal-scan -f
```
