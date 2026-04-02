# Configuration Notes

This file captures the simplification direction for `config/server.yaml`.

Current status:

- deployment-wiring values such as API bind host/port and public service URLs now default in code
- tracked `server.yaml` keeps shared execution policy and contract wiring
- most scanner, pricing, and API tuning knobs now default in code instead of the tracked file

## Core Model

The useful split is not just secret vs non-secret.

Use three buckets:

- `server.yaml`: shared, reviewable application policy
- `.env`: deployment-local wiring and credentials
- code defaults: expert tuning and escape hatches

Another way to say it:

- `server.yaml` answers: "What should this Tidal instance do?"
- `.env` answers: "How does this host/process reach its dependencies?"
- defaults answer: "What should happen if nobody explicitly cares?"

## What Belongs In `server.yaml`

These are strong candidates to keep in tracked config because they are shared application behavior:

- `chain_id`
- `monitored_fee_burners`
- the `kick:` policy block
  - default profile
  - profiles
  - profile overrides
  - ignore rules
  - cooldown rules
  - other real execution policy

If a value should be reviewed in PRs and should travel with the repo, it probably belongs here.

## What Probably Belongs In `.env`

These are often better treated as host-local or deployment-local wiring, even when they are not secret:

- `RPC_URL`
- `DB_PATH`
- `TIDAL_HOME`
- `TIDAL_ENV_FILE`
- `TIDAL_API_KEY`
- `TXN_KEYSTORE_PATH`
- `TXN_KEYSTORE_PASSPHRASE`

These may also belong in `.env` if they vary by deployment rather than by shared app policy:

- `tidal_api_host`
- `tidal_api_port`
- `token_price_agg_base_url`
- `auctionscan_base_url`
- `auctionscan_api_base_url`

The rule of thumb is:

- if the value belongs to a machine, container, or deployment environment, prefer `.env`
- if the value exists mostly so one operator can wire a host differently, prefer `.env`

## What Probably Should Not Be In `server.yaml` By Default

These look more like tuning knobs than repo-owned policy:

- `rpc_timeout_seconds`
- `rpc_retry_attempts`
- `scan_concurrency`
- `price_timeout_seconds`
- `price_retry_attempts`
- `price_concurrency`
- `price_delay_seconds`
- `multicall_discovery_batch_calls`
- `multicall_rewards_batch_calls`
- `multicall_rewards_index_max`
- `multicall_balance_batch_calls`
- `multicall_overflow_queue_max`
- `multicall_auction_batch_calls`
- `tidal_api_receipt_reconcile_interval_seconds`
- `tidal_api_receipt_reconcile_threshold_seconds`

These are good candidates for:

- code defaults
- optional env overrides when debugging or tuning

The file feels heavy largely because these values are exposed at all times, even though most operators should never need to touch them.

## Good Candidates For Re-grouping

Some settings may still belong in tracked config, but should be nested under `kick:` so the file reads more cleanly:

- `auction_kicker_address`
- `txn_usd_threshold`
- `txn_max_base_fee_gwei`
- `txn_max_priority_fee_gwei`
- `txn_max_gas_limit`
- `txn_start_price_buffer_bps`
- `txn_min_price_buffer_bps`
- `txn_quote_spot_warning_threshold_pct`
- `txn_max_data_age_seconds`
- `txn_require_curve_quote`
- `max_batch_kick_size`
- `batch_kick_delay_seconds`

This may not reduce the absolute count much, but it reduces top-level noise and makes the file easier to reason about.

## Possible Simplification Target

If the goal is a much lighter `server.yaml`, a reasonable target is:

- `chain_id`
- `monitored_fee_burners`
- `kick:`

Possible optional keepers:

- `tidal_api_host`
- `tidal_api_port`

Everything else becomes either:

- `.env` deployment wiring
- code defaults
- env-only escape hatches for advanced tuning

## Decision Rule

When deciding where a setting should live:

1. If it is shared behavior and should be code-reviewed, keep it in `server.yaml`.
2. If it is deployment wiring or machine-local, move it to `.env`.
3. If it is mostly an implementation detail or rare tuning knob, keep it out of tracked config and rely on defaults.

## Recommended Order Of Operations

If we simplify this later, the lowest-risk order is:

1. Remove tuning knobs from tracked config first.
2. Collapse execution-related settings under `kick:`.
3. Decide whether bind addresses and external service URLs are repo-owned or deployment-owned.
4. Leave true business policy in `server.yaml`.
