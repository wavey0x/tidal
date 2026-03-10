# Tidal Dashboard Refactor Plan

## Goals

- Keep SQLite as the only datastore and cache source of truth.
- Remove ad hoc JSON/file caches (`strategy_auction_map.json`, `ui/.cache/token-logos/`).
- Make all dashboard-relevant data readable from SQLite alone, so a separate read API can serve it.
- Host the React UI on Vercel free tier as a static app.
- Remove native token logo proxy/cache logic from this repo.
- Source token logos from `prices.wavey.info` and cache only validated logo URLs in SQLite.

## Confirmed External Dependency Shape

Verified on March 10, 2026:

- `GET https://prices.wavey.info/v1/price?token=<address>&chain_id=1`
- Price still comes from `summary.high_price`
- Candidate logo URL is exposed at `token.logo_url`

Observed relevant response shape:

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

This means the scanner should continue using `summary.high_price` for USD price persistence and should treat `token.logo_url` as the only first-party logo candidate source.

## Current State

- The scanner writes latest data into SQLite:
  - `vaults`
  - `strategies`
  - `tokens`
  - `strategy_tokens`
  - `strategy_token_balances_latest`
  - `scan_runs`
  - `scan_item_errors`
- Strategy-to-auction mapping is currently persisted as `strategy_auction_map.json`.
- The UI API is a separate Express app that:
  - shells out to `sqlite3`
  - reads the auction JSON file directly
  - proxies and caches token logos on local disk and in memory
- The React UI fetches separate endpoints for summary, token catalog, and strategy rows.

This is the wrong boundary for Vercel because the current read path depends on local filesystem state and a colocated Node process.

## Target Architecture

### Write path

- Keep the existing Python scanner as the writer.
- Continue scanning on a small non-Vercel machine.
- Continue storing canonical latest state in SQLite.
- Move the strategy-to-auction cache into SQLite.
- Cache validated token logo URLs in SQLite.

### Read path

- A separate project will serve a read-only API from the same SQLite file (`api.wavey.info`).
- The API is not part of this repo. This repo's responsibility ends at making SQLite self-contained.
- The UI consumes one endpoint:
  - `GET /v1/dashboard`
- Optional supporting endpoint:
  - `GET /v1/health`

### Frontend hosting

- Vercel serves only the static UI build.
- The UI fetches the dashboard JSON from `api.wavey.info`, ideally through a Vercel rewrite to preserve same-origin `/api/*` calls.

## Data Model Changes

### 1. Move auction cache into SQLite

Recommended approach: store latest auction mapping on `strategies`, not in a new table.

Rationale:

- The current use case is a single latest auction address per strategy.
- The UI only needs the latest resolved mapping.
- This keeps the dashboard query simple and removes the JSON sidecar entirely.

Add columns to `strategies`:

- `auction_address TEXT NULL`
- `auction_updated_at TEXT NULL`
- `auction_error_message TEXT NULL`

Behavior:

- `auction_address` is the latest valid mapping, or `NULL`.
- `auction_updated_at` is when the mapping was last refreshed.
- `auction_error_message` records the latest refresh failure for observability without making the JSON file necessary.

### 2. Cache validated logo URLs on `tokens`

Add columns to `tokens`:

- `logo_url TEXT NULL`
- `logo_source TEXT NULL`
- `logo_status TEXT NULL`
- `logo_validated_at TEXT NULL`
- `logo_error_message TEXT NULL`

Recommended status values:

- `SUCCESS`
- `NOT_FOUND`
- `INVALID`
- `FAILED`

Behavior:

- `logo_url` is only populated after validation succeeds.
- `logo_source` should initially be `token_price_agg_logo_url`.
- `logo_status` and `logo_error_message` help explain why a token still has no logo URL.
- Do not overwrite a non-empty `logo_url` in phase 1.

### 3. SQLite concurrency hardening

Because the scanner and a separate read API will access the same DB file on one machine:

- Enable SQLite WAL mode.
- Set a reasonable `busy_timeout`.
- Prefer `synchronous=NORMAL`.

Implementation details:

- WAL mode is a persistent database-level setting. Set it once via an Alembic migration (`PRAGMA journal_mode=WAL`), not per-connection.
- `busy_timeout` and `synchronous` are per-connection. Set them via a SQLAlchemy `event.listen("connect", ...)` handler in `db.py`.
- The external read API must also set `busy_timeout` on its own connections.

## Backend Refactor Plan

### Phase 0. Preparatory cleanup

1. Add `ui/.cache/token-logos/` to `.gitignore`.
2. Remove the cached `.miss` files from tracking: `git rm -r --cached ui/.cache/token-logos/`.
3. Add `strategy_auction_map.json` to `.gitignore` (will be removed from repo after Phase 2 cutover).

### Phase 1. Schema and persistence

1. Add a new Alembic migration:
   - Add the auction fields to `strategies`.
   - Add the logo fields to `tokens`.
   - Enable WAL mode (`PRAGMA journal_mode=WAL`).
   - Include a `downgrade()` that drops the new columns (WAL mode is left as-is on downgrade).
2. Update SQLAlchemy models.
3. Add per-connection pragmas in `db.py` via `event.listen("connect", ...)`:
   - `PRAGMA busy_timeout=5000`
   - `PRAGMA synchronous=NORMAL`
4. Add repository methods to:
   - persist strategy auction mapping rows
   - read cached auction mapping from SQLite
   - persist validated logo URL metadata

### Phase 2. Replace JSON auction cache with SQLite

Refactor `StrategyAuctionMapper` so it no longer writes or reads `strategy_auction_map.json`.

New behavior:

1. Resolve strategy -> auction mapping exactly as today.
2. Persist the latest result into `strategies.auction_address`.
3. Persist `auction_updated_at` after each refresh attempt.
4. On refresh failure:
   - record a scan item error
   - leave existing `auction_address` values untouched
   - use the previously persisted SQLite values as the fallback cache

Required cleanup:

- Remove `AUCTION_CACHE_PATH` from `config.py` (`Settings` class).
- Remove `AUCTION_CACHE_PATH` from `.env.example`.
- Remove JSON read/write logic from `auction_mapper.py`.
- Remove `auction_cache_path` wiring from `runtime.py`.
- Remove `TIDAL_AUCTION_CACHE_PATH` / `AUCTION_CACHE_PATH` references from `ui/server/index.mjs` and `ui/README.md`.
- Remove `strategy_auction_map.json` from the repo.
- Update test fixtures in `tests/unit/test_strategy_auction_mapper.py` and `tests/integration/test_scanner_service.py` that mock or reference the JSON file.

Note: the JSON file currently also stores metadata (`chainId`, `factoryAddress`, `requiredGovernanceAddress`, `selectionRule`). This metadata is already available in `config.py` and `constants.py` and does not need to be persisted separately. It is intentionally dropped.

### Phase 3. Extend price refresh to backfill logo URLs

Refactor the price provider so it returns both:

- price quote data from `summary.high_price`
- candidate logo URL from `token.logo_url`

Recommended internal shape:

```python
TokenPriceQuote(
    price_usd=Decimal(...),
    quote_amount_in_raw=1,
    logo_url="https://...",
)
```

Add logo validation logic to the scanner-side refresh flow:

1. Only attempt validation when:
   - the token row has `logo_url IS NULL`, AND
   - `logo_status IS NULL` (never attempted), OR
   - `logo_status IN ('FAILED', 'NOT_FOUND', 'INVALID')` and `logo_validated_at` is older than a retry interval.
   - This bounds work per scan to only new or retry-eligible tokens.
   - Recommended retry policy:
     - `FAILED`: retry after 24 hours
     - `NOT_FOUND`: retry after 7 days
     - `INVALID`: retry after 7 days
   - `SUCCESS` rows are not retried unless explicitly cleared or backfilled by a future migration/tooling pass.
2. Read candidate URL from `payload["token"]["logo_url"]`.
3. If missing or empty:
   - store `logo_status = NOT_FOUND`
   - leave `logo_url = NULL`
4. If present:
   - make a validation request
   - require final HTTP status `200`
   - require `Content-Type` to start with `image/`
   - reject `404`, `5xx`, timeouts, redirects to non-image content, and non-image payloads
5. On success:
   - persist `logo_url`
   - persist `logo_source`
   - persist `logo_status = SUCCESS`
   - persist `logo_validated_at`
6. On failure:
   - keep `logo_url = NULL`
   - persist `logo_status` and `logo_error_message`

Implementation note:

- Prefer a streaming `GET` rather than `HEAD`, since many asset endpoints handle `HEAD` poorly.
- Use `httpx` (already a dependency) with `client.stream("GET", url)` and check `response.headers` before reading the body.
- Close the response as soon as headers are validated; there is no need to store image bytes locally.

### Phase 4. SQLite contract for the external read API

The dashboard API lives in a separate project. This repo's job is to ensure the SQLite schema is self-contained and well-defined so the external reader can depend on it.

The external API may depend on the following tables and columns:

| Table | Columns the API reads |
|-------|----------------------|
| `vaults` | `address`, `name`, `symbol` |
| `strategies` | `address`, `name`, `vault_address`, `auction_address`, `auction_updated_at` |
| `tokens` | `address`, `symbol`, `name`, `decimals`, `price_usd`, `price_source`, `logo_url` |
| `strategy_tokens` | `strategy_address`, `token_address` |
| `strategy_token_balances_latest` | `strategy_address`, `token_address`, `raw_balance`, `normalized_balance`, `scanned_at` |
| `scan_runs` | `run_id`, `started_at`, `finished_at`, `status`, `vaults_seen`, `strategies_seen`, `pairs_seen`, `pairs_succeeded`, `pairs_failed`, `error_summary` |

The API should join these to produce a single dashboard payload. Key relationships:

- `strategies.vault_address` -> `vaults.address`
- `strategy_token_balances_latest.strategy_address` -> `strategies.address`
- `strategy_token_balances_latest.token_address` -> `tokens.address`
- `strategies.auction_address` is the resolved auction mapping (nullable)
- `tokens.logo_url` is the validated logo URL (nullable)

SQLite requirements for the external reader:

- WAL mode is enabled (set by this repo's migration).
- The reader must set `PRAGMA busy_timeout` on its own connections.
- The reader should treat the database as read-only.

### Phase 5. Remove the Node read server

After the external dashboard API is live and the UI is cut over:

- Delete `ui/server/index.mjs`
- Remove `express` and `cors` from `ui/package.json`
- Remove the `dev:server` and `start` npm scripts that launch the Express server
- Change `dev` so it no longer starts `dev:server`; keep `preview` as plain `vite preview`
- Remove `ui/.cache/token-logos/` directory entirely
- Update `vite.config.js`: remove any Express-specific dev proxy; add a proxy to the external API for local development (e.g., `/api -> http://localhost:<api-port>`)

Note: this phase is blocked on the external API project being deployed. It cannot be completed within this repo alone.

## Frontend Refactor Plan

### 1. Switch from multiple endpoints to one dashboard endpoint

Change the UI to load one payload instead of:

- `/api/summary`
- `/api/tokens`
- `/api/strategy-balances`
- `/api/token-logo/:address`

New flow:

- Fetch `/api/dashboard` or `/v1/dashboard`
- Populate summary, tokens, and rows from that single payload

### 2. Remove native logo logic

Delete all repo-owned token logo serving/caching behavior.

The UI should:

- render `balance.tokenLogoUrl` directly
- hide the image on error as it already does
- never call an internal `/api/token-logo/*` endpoint again

### 3. Make API origin configurable for Vercel

Recommended options:

- Preferred: Vercel rewrite `/api/:path* -> https://api.wavey.info/v1/:path*`
- Alternate: `VITE_TIDAL_API_BASE_URL=https://api.wavey.info/v1`

Preferred outcome:

- production UI keeps same-origin `/api/dashboard`
- browser CORS handling is avoided
- local dev can continue using a simple proxy

### 4. Preserve UI behavior

The following should remain unchanged:

- USD mode default
- 2 decimal formatting
- hidden entries below `$0.01` when priced
- `?` for unknown USD values
- existing theme behavior and contrast guardrails

## Rollout Plan

### Step 1. Preparatory cleanup (Phase 0)

- Add `ui/.cache/token-logos/` and `strategy_auction_map.json` to `.gitignore`.
- Remove cached `.miss` files from git tracking.

### Step 2. Schema migration (Phase 1)

- Add new Alembic revision for auction fields, logo fields, and WAL mode.
- Update SQLAlchemy models and repositories.
- Add per-connection pragmas in `db.py`.

### Step 3. Scanner cutover (Phases 2 + 3)

- Persist auction mapping into SQLite.
- Stop reading/writing `strategy_auction_map.json`.
- Remove `AUCTION_CACHE_PATH` from config, runtime, `.env.example`, and tests.
- Extend price refresh to return candidate logo URL.
- Validate and persist missing logo URLs.

### Step 4. UI cutover + Express removal (Phase 5) [blocked on external API]

- Replace multiple fetches with one dashboard fetch.
- Remove `/api/token-logo/*` usage.
- Add configurable API base or Vercel rewrite.
- Remove the Express server and related npm scripts.
- Remove `ui/.cache/token-logos/` directory.

### Step 5. Final cleanup

- Remove `strategy_auction_map.json` from repo.
- Remove JSON cache references from README files.
- Verify `.gitignore` is clean.

## Testing and Verification

### Backend

- Run Alembic migrations against a fresh DB and an existing DB.
- Run scanner integration tests.
- Add tests for:
  - auction fallback from persisted SQLite data
  - valid logo URL persisted once when previously missing
  - invalid logo URL not persisted
  - non-image URL rejected
  - existing `tokens.logo_url` not overwritten in phase 1

### SQLite contract

- Verify that the tables and columns listed in Phase 4 are present after migration.
- Verify concurrent read access works while a scan daemon is writing (WAL mode).

### Frontend

- Run `npm run build` in `/Users/wavey/yearn/tidal/ui`
- Check light theme visually
- Check dark theme visually
- Confirm token logo rendering still degrades cleanly on broken image URLs
- Confirm current accessibility guardrails remain intact

## Operational Notes

- The scanner and the external read API run on the same machine against the same SQLite file.
- WAL mode allows concurrent reads while the scanner writes.
- If read load grows later, the next optimization is not Postgres by default.
- The next optimization would be either:
  - stronger HTTP caching on the dashboard endpoint
  - a materialized dashboard table inside SQLite
  - a pre-rendered dashboard JSON artifact generated from SQLite

## Out of Scope

- Postgres
- Vercel server-side DB access
- Local disk logo caching
- Repo-owned token logo proxying
- Historical dashboard snapshots beyond what already exists in scan tables
- The dashboard read API (lives in a separate project)

## Acceptance Criteria

- SQLite is the only datastore and cache source of truth.
- There is no `strategy_auction_map.json` dependency in the scanner.
- `AUCTION_CACHE_PATH` is removed from config, `.env.example`, and all code paths.
- A valid logo URL from `prices.wavey.info` is persisted to `tokens.logo_url` when missing.
- Invalid or non-image logo URLs are rejected and not persisted as `logo_url`.
- Non-success logo validations are retried on a bounded schedule, not every scan cycle.
- WAL mode is enabled and concurrent reads work while the scanner writes.
- The SQLite schema is self-documenting enough for the external API to read without importing this repo's code.
- There is no internal `/api/token-logo/*` endpoint (after UI cutover).
- The UI can be deployed to Vercel and fetch dashboard data from `api.wavey.info` (after UI cutover).
