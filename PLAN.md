# Tidal Dashboard Refactor Plan

## Summary

Ship one cutover with no compatibility layer. After merge, the scanner writes all dashboard state to SQLite, `api.wavey.info` serves `GET /v1/dashboard` from SQLite, and the Vercel UI reads only that endpoint.

Delete the JSON/file cache path and the local Node read server in the same change. No dual-read period, no legacy endpoints kept alive.

SQLite is the only datastore. Mainnet only (`chain_id=1`). The dashboard read API lives in a separate project. No backward compatibility is required.

## Confirmed External Dependency Shape

Verified on March 10, 2026:

- `GET https://prices.wavey.info/v1/price?token=<address>&chain_id=1`
- Price comes from `summary.high_price`
- Candidate logo URL is at `token.logo_url`

```json
{
  "chain_id": 1,
  "token": {
    "address": "0x...",
    "symbol": "CRV",
    "decimals": 18,
    "logo_url": "https://assets.smold.app/api/token/1/0x.../logo-128.png"
  },
  "price_data": {
    "provider": "curve",
    "price": "0.24562493920923972"
  },
  "summary": {
    "high_price": "0.246543"
  }
}
```

## Scanner + SQLite

### Schema (already migrated)

The following columns already exist in models and the database:

- `strategies`: `auction_address`, `auction_updated_at`
- `tokens`: `logo_url`, `logo_status`, `logo_validated_at`, `logo_error_message`

WAL mode is enabled. Per-connection pragmas (`busy_timeout`, `synchronous=NORMAL`) are set in `db.py`.

### Scanner work (implemented)

All scanner logic is implemented and tested:

- Auction mappings persist to `strategies.auction_address` via `StrategyRepository.set_auction_mappings()`.
- On auction refresh failure: previous `auction_address` is preserved, `auction_updated_at` is updated, error recorded in `scan_item_errors`.
- Logo validation via `TokenLogoValidator`: streaming GET, content-type check, four status codes (`SUCCESS`, `NOT_FOUND`, `INVALID`, `FAILED`).
- Logo retry logic with configurable backoff intervals for non-success statuses.
- Non-null `logo_url` is never overwritten automatically.
- Price-alias behavior propagates results across alias token rows.
- `AUCTION_CACHE_PATH` and all JSON read/write logic removed.
- `strategy_auction_map.json` is in `.gitignore` (file still on disk; can be deleted).

### Remaining cleanup

- Delete `strategy_auction_map.json` from the working directory.

## External API Contract

The external API lives in a separate project. This repo's responsibility ends at making SQLite self-contained.

SQLite tables the external API depends on:

| Table | Columns |
|-------|---------|
| `vaults` | `address`, `name`, `symbol` |
| `strategies` | `address`, `name`, `vault_address`, `auction_address`, `auction_updated_at` |
| `tokens` | `address`, `symbol`, `name`, `decimals`, `price_usd`, `price_source`, `logo_url` |
| `strategy_token_balances_latest` | `strategy_address`, `token_address`, `raw_balance`, `normalized_balance`, `scanned_at` |
| `scan_runs` | `run_id`, `started_at`, `finished_at`, `status`, `vaults_seen`, `strategies_seen`, `pairs_seen`, `pairs_succeeded`, `pairs_failed`, `error_summary` |

The external API must set `busy_timeout` on its own connections and treat the database as read-only.

## UI + Deployment (implemented)

All UI work is complete:

- Express server deleted. `express` and `cors` removed from `package.json`.
- npm scripts cleaned: only `dev`, `build`, `preview` remain.
- `App.jsx` fetches only `/api/dashboard`.
- Vite proxy configured via `TIDAL_API_PROXY_TARGET` env var.
- Token logos render directly from `tokenLogoUrl`. No proxy, no disk cache.
- `ui/.cache/token-logos/` is in `.gitignore`.

## Test Coverage

All tests pass (47 passed, 1 skipped):

- Scanner integration tests: auction persistence + fallback on mapper failure.
- Price/logo service tests: validation, caching skip, retry after backoff, alias propagation.
- Logo validator unit tests: success, 404, 500, non-image content-type, missing content-type, SVG, connection error, invalid scheme, null/empty input.

## Out of Scope

- Postgres
- Vercel server-side DB access
- Local disk logo caching or repo-owned logo proxying
- Historical dashboard snapshots
- The dashboard read API (separate project)
