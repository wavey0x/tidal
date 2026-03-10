# Tidal UI

React dashboard for viewing scan results from an external read-only dashboard API.

## What it shows

- One row per strategy with stacked token balances
- Per-strategy auction column
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

Configure the dashboard API with either:

```bash
VITE_TIDAL_API_BASE_URL=https://api.wavey.info/v1 npm run dev
```

or by keeping the default `/api` base path and proxying locally:

```bash
TIDAL_API_PROXY_TARGET=http://localhost:8787 npm run dev
```

## Endpoints

- `GET /api/dashboard`
