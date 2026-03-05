# Tidal UI

React dashboard + lightweight Node API for viewing `tidal.db` scan results.

## What it shows

- One row per strategy with stacked token balances
- Per-strategy auction column (from strategy-auction cache JSON)
- Token filter + address/symbol search
- Checksummed addresses
- Balances formatted to 2 decimals
- Token logos via SmolDapp tokenAssets with TrustWallet fallback, plus local disk+memory cache

## Run locally

1. Install dependencies:
   ```bash
   cd ui
   npm install
   ```
2. Start frontend + API:
   ```bash
   npm run dev
   ```
3. Open `http://localhost:5173`

The API defaults to reading `../tidal.db`.
Override with:

```bash
TIDAL_DB_PATH=/absolute/path/to/tidal.db npm run dev:server
```

Strategy-auction cache path defaults to a sibling of the DB file (`strategy_auction_map.json`).
Override with:

```bash
TIDAL_AUCTION_CACHE_PATH=/absolute/path/to/strategy_auction_map.json npm run dev:server
```

`AUCTION_CACHE_PATH` is also supported for parity with scanner settings.

## Endpoints

- `GET /api/health`
- `GET /api/summary`
- `GET /api/tokens`
- `GET /api/strategy-balances?limit=<n>`
- `GET /api/balances?token=<address>&limit=<n>`
- `GET /api/token-logo/<tokenAddress>?chainId=1`
