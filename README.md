# Tidal (Phase 1)

CLI service for discovering Yearn strategies and caching reward token balances in SQLite.

## Quick Start

1. Create and activate a Python 3.12+ virtualenv.
2. Install:
   ```bash
   pip install -e .[dev]
   ```
3. Copy `.env.example` to `.env` and set `RPC_URL`.
4. Run migrations:
   ```bash
   tidal db migrate
   ```
5. Run one scan:
   ```bash
   tidal scan once
   ```

## Commands

- `tidal db migrate`
- `tidal scan once`
- `tidal scan daemon --interval-seconds 300`
- `tidal healthcheck`

## UI Dashboard

A React dashboard is available in [`ui/`](./ui) for browsing scan results from `tidal.db`.

```bash
cd ui
npm install
npm run dev
```

This starts:

- Frontend: `http://localhost:5173`
- API server: `http://localhost:8787`

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

## Strategy Auction Mapping Cache

Each scan also refreshes a strategy-to-auction cache at `strategy_auction_map.json` (sibling of the DB file by default).
The cache maps strategies to auctions by matching `strategy.want()` with `auction.want()` for auctions returned by:

- `getAllAuctions()` on `AUCTION_FACTORY_ADDRESS` (default `0xe87af17acba165686e5aa7de2cec523864c25712`)

Only auctions with governance `0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b` are considered matches.

Optional env overrides:

- `AUCTION_FACTORY_ADDRESS`
- `AUCTION_CACHE_PATH`
- `MULTICALL_AUCTION_BATCH_CALLS`
