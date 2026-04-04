# Tidal UI

React dashboard for the monorepo Tidal control-plane API.

## What it shows

- A `Strategies` tab with one row per strategy and stacked token balances
- A dedicated `Fee Burner` tab with fee burner identity, auction, want token, and approved token balances
- A shared `Kick Log` tab keyed by source
- Token filter + address/symbol search
- Balances formatted to 2 decimals
- Token logos rendered directly from validated `tokenLogoUrl` values in the dashboard payload

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

Dashboard and log reads are public. To call authenticated endpoints (prepare/broadcast), set:

```bash
VITE_TIDAL_API_KEY=your-key npm run dev
```

## Endpoints

- `GET /api/v1/tidal/dashboard`
- `GET /api/v1/tidal/logs/kicks`
- `POST /api/v1/tidal/auctions/deploy/prepare`
