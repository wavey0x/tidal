# Tidal

Monorepo for the Yearn tidal. It contains the scanner that builds the dataset, the transaction service that acts on it, the dashboard UI, and the supporting contracts and schema shared across those pieces.

## Monorepo Layout

- `tidal/scanner/`: discovers Yearn vaults and strategies, reads configured fee burners, resolves sell tokens, reads balances, refreshes token prices and auction mappings, and writes the latest dashboard state into SQLite.
- `tidal/transaction_service/`: reads the scanner's cached state, selects kick candidates across strategies and fee burners, estimates and submits auction kick transactions, and records transaction runs back into SQLite.
- `ui/`: React dashboard that renders the cached strategy, fee burner, token, vault, and auction data from the read-only API.
- `contracts/`: Foundry project for the on-chain `AuctionKicker` helper contract and its deployment/test scripts.
- `tidal/persistence/` and `alembic/`: shared database models, repositories, and migrations used by the scanner and transaction service.
- `tidal/chain/`, `tidal/pricing/`, and `tidal/runtime.py`: shared chain readers, pricing integrations, and service wiring used across the backend components.

Out of scope for this repo:

- the read-only dashboard API that serves `GET /tidal` from the SQLite database lives separately

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
3. Edit `config.yaml` to configure operational settings. Copy `.env.example` to `.env` and set `RPC_URL`.
4. Run migrations:
   ```bash
   tidal db migrate
   ```
5. Run one scan:
   ```bash
   tidal scan
   ```

## Auction Pricing Policy

The transaction service reads pricing-profile overrides from [`auction_pricing_policy.yaml`](./auction_pricing_policy.yaml) at the repo root.

Use it to mark specific `auction -> sell token` combinations as `stable`. Anything not listed there falls back to the `default_profile`, which should usually stay `volatile`.

Minimal shape:

```yaml
default_profile: volatile

profiles:
  volatile:
    start_price_buffer_bps: 1000
    min_price_buffer_bps: 500
    step_decay_rate_bps: 50

  stable:
    start_price_buffer_bps: 100
    min_price_buffer_bps: 50
    step_decay_rate_bps: 1

auctions:
  0xauction_address:
    0xsell_token_address: stable
```

Fill it out with these rules:

- only add entries under `auctions` when you want non-default behavior
- the key is the auction address, then the sell token address for that auction
- the value is the profile name, usually `stable`
- most auctions should have no entry at all
- no auto-classification exists in v1
- do not put these mappings in `config.yaml`

Examples:

- `USDC/USDT` style auction lots can be marked `stable`
- `WETH/wstETH` style auction lots can be marked `stable`
- if an auction sell token is not listed, it will use the `volatile` profile

## Commands

- `tidal db migrate`
- `tidal scan`
- `tidal scan daemon --interval-seconds 300`
- `tidal txn` — dry-run evaluation of kick candidates
- `tidal txn --live` — evaluate and send individual kick() transactions per candidate
- `tidal txn --confirm` — interactive confirmation before each kick (implies `--live`)
- `tidal txn --live --source 0x...` — target a specific source address
- `tidal txn --live --auction 0x...` — target a specific auction address
- `tidal txn --live --batch` — send a single batchKick() transaction (all-or-nothing)
- `tidal txn daemon --live` — run the transaction service continuously (uses batchKick by default)
- `tidal txn daemon --live --no-batch` — daemon with individual kick() per candidate
- `tidal healthcheck`

Shortlist behavior: only the highest-USD token per auction is kickable in a single evaluation cycle. Additional above-threshold tokens on the same auction stay deferred until a later run, because the auction can only carry one active lot at a time.
Targeted `--source` and `--auction` filters are applied before that per-auction collapse.

## UI Dashboard

A React dashboard in [`ui/`](./ui) is deployed to Vercel as a static site. API calls are rewritten to the external dashboard API via `vercel.json`:

```json
{
  "rewrites": [
    { "source": "/api/:path*", "destination": "https://api.wavey.info/tidal/:path*" }
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

- set `VITE_TIDAL_API_BASE_URL` to your external dashboard API, or
- keep the default `/api` base path and point the Vite proxy at your local API with `TIDAL_API_PROXY_TARGET`

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

Price refresh is bounded by `price_concurrency`, `price_timeout_seconds`, and `price_retry_attempts` in `config.yaml`.

The pricing endpoint and API key are configured via `token_price_agg_base_url` in `config.yaml` and `TOKEN_PRICE_AGG_KEY` in `.env`.

Each scan also backfills validated token logo URLs into `tokens.logo_url` using `token.logo_url` from the same price response.

## Strategy Auction Mapping

Each scan refreshes strategy and fee burner auction mappings directly into persistence.
Auctions are fetched via `getAllAuctions()` on the `auction_factory_address` configured in `config.yaml`.

- Strategy matches are resolved by comparing each auction's `receiver` with the strategy address.
- Fee burner matches are resolved by comparing `(auction.receiver, auction.want)` with the configured `(fee burner address, want_address)` pair from `monitored_fee_burners` in `config.yaml`.

The `auction_version` field tracks the factory that produced each auction.

Tuning knobs: `auction_factory_address` and `multicall_auction_batch_calls` in `config.yaml`.

## Dashboard API

The scanner writes all dashboard state to SQLite. A separate read-only API serves `GET /tidal` from the same database file.

See [`EXTERNAL_PLAN.md`](./EXTERNAL_PLAN.md) for the full endpoint spec, confirmed schema, SQL queries, and response shape.

Key tables the API reads:

| Table | Purpose |
|-------|---------|
| `vaults` | Vault name and symbol |
| `strategies` | Strategy name, vault FK, `auction_address` |
| `fee_burners` | Fee burner name, `want_address`, `auction_address` |
| `tokens` | Symbol, name, `price_usd`, `logo_url` |
| `strategy_token_balances_latest` | Latest normalized balances per strategy-token pair |
| `fee_burner_token_balances_latest` | Latest normalized balances per fee burner-token pair |
| `scan_runs` | Scan metadata for diagnostics |

SQLite concurrency:

- WAL mode is enabled by this repo's migration.
- The API should open the database in read-only mode with `busy_timeout` set.
