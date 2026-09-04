# Tidal UI

React dashboard for the monorepo Tidal control-plane API.

## What it shows

- A `Strategies` tab with one row per strategy and stacked token balances
- A dedicated `Fee Burner` tab with fee burner identity, auction, want token, and approved token balances
- A shared `Kick Log` tab keyed by source
- Token filter + address/symbol search
- Balances formatted to 2 decimals
- Token logos loaded lazily from first-party `tokenLogoUrl` resources, with a bundled placeholder for unavailable images

## Run locally

1. Install dependencies:
   ```bash
   cd ui
   npm install
   ```
2. Start the frontend:
   ```bash
   npm run dev
   ```
3. Open `http://localhost:5173`

By default, local development proxies `/api/v1/tidal` to the production API:

```bash
TIDAL_API_PROXY_TARGET=https://api.tidal.wavey.info
```

Override the dashboard API explicitly when needed:

```bash
VITE_TIDAL_API_BASE_URL=https://api.tidal.wavey.info/api/v1/tidal npm run dev
```

Or point the dev server at a local API proxy:

```bash
TIDAL_API_PROXY_TARGET=http://localhost:8787 npm run dev
```

All current browser endpoints are public, including wallet deployment preparation.
The browser never needs an operator API key. Keep operator credentials in the CLI;
do not put secrets in frontend environment variables. Only the two documented
public API base URL variables are exposed to browser code, in development and production.

## Endpoints

- `GET /api/v1/tidal/dashboard`
- `GET /api/v1/tidal/logs/kicks`
- `GET /api/v1/tidal/alerts`
- `GET /api/v1/tidal/strategies/{strategy}/deploy-defaults`
- `POST /api/v1/tidal/auctions/deploy/browser-prepare`
