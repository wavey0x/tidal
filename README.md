# Factory Dashboard

CLI service for discovering Yearn strategies and caching reward token balances in SQLite.

## Quick Start

1. Create and activate a Python 3.12+ virtualenv:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
2. Install:
   ```bash
   pip install -e ".[dev]"
   ```
3. Copy `.env.example` to `.env` and set `RPC_URL`.
4. Run migrations:
   ```bash
   factory-dashboard db migrate
   ```
5. Run one scan:
   ```bash
   factory-dashboard scan once
   ```

## Commands

- `factory-dashboard db migrate`
- `factory-dashboard scan once`
- `factory-dashboard scan daemon --interval-seconds 300`
- `factory-dashboard healthcheck`

## UI Dashboard

A React dashboard in [`ui/`](./ui) is deployed to Vercel as a static site. API calls are rewritten to the external dashboard API via `vercel.json`:

```json
{
  "rewrites": [
    { "source": "/api/:path*", "destination": "https://api.wavey.info/factory-dashboard/:path*" }
  ]
}
```

Deploy by connecting the `ui/` directory to a Vercel project (root directory = `ui`).

### Local development

```bash
cd ui
npm install
npm run dev
```

For local dev, either:

- set `VITE_FACTORY_DASHBOARD_API_BASE_URL` to your external dashboard API, or
- keep the default `/api` base path and point the Vite proxy at your local API with `FACTORY_DASHBOARD_API_PROXY_TARGET`

## Multicall Batching

Multicall3 is enabled by default and used for:

1. `withdrawalQueue[0..3]` discovery per vault
2. indexed `rewardsTokens(i)` probing per strategy (stop on first failure/zero)
3. `balanceOf(strategy)` per strategy-token pair

Fallback to direct calls is automatic when a multicall chunk fails.

## Price Refresh

Each scan refreshes USD prices once per unique token discovered in that scan and stores the latest quote fields directly on `tokens`.
The source endpoint is:

- `https://prices.wavey.info/v1/price?token=<token_address>&chain_id=1`

The scanner uses `summary.high_price` from the response as the persisted USD value.

Price refresh is bounded by:

- `PRICE_CONCURRENCY`
- `PRICE_TIMEOUT_SECONDS`
- `PRICE_RETRY_ATTEMPTS`

Optional pricing env overrides:

- `TOKEN_PRICE_AGG_BASE_URL`
- `TOKEN_PRICE_AGG_KEY`

Each scan also backfills validated token logo URLs into `tokens.logo_url` using `token.logo_url` from the same price response.

## Strategy Auction Mapping

Each scan refreshes strategy-to-auction mappings directly into the `strategies` table.
Mappings are resolved by matching `strategy.want()` with `auction.want()` for auctions returned by:

- `getAllAuctions()` on `AUCTION_FACTORY_ADDRESS` (default `0xe87af17acba165686e5aa7de2cec523864c25712`)

Only auctions with governance `0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b` are considered matches.

Optional env overrides:

- `AUCTION_FACTORY_ADDRESS`
- `MULTICALL_AUCTION_BATCH_CALLS`

## Dashboard API

The scanner writes all dashboard state to SQLite. A separate read-only API serves `GET /factory-dashboard` from the same database file.

See [`EXTERNAL_PLAN.md`](./EXTERNAL_PLAN.md) for the full endpoint spec, confirmed schema, SQL queries, and response shape.

Key tables the API reads:

| Table | Purpose |
|-------|---------|
| `vaults` | Vault name and symbol |
| `strategies` | Strategy name, vault FK, `auction_address` |
| `tokens` | Symbol, name, `price_usd`, `logo_url` |
| `strategy_token_balances_latest` | Latest normalized balances per strategy-token pair |
| `scan_runs` | Scan metadata for diagnostics |

SQLite concurrency:

- WAL mode is enabled by this repo's migration.
- The API should open the database in read-only mode with `busy_timeout` set.
