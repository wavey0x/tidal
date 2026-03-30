# CLI Client Guide

## Role Of The CLI Client

`tidal` is the API-backed CLI client. It does not own the shared database. It reads and prepares actions through the control-plane API, then signs and broadcasts transactions locally.

If Tidal is not installed on this machine yet, start with [Install](install.md).

That split matters:

- the server owns shared state and audit history
- the CLI client owns private-key access

## Required Environment

Run `tidal init` once first. That scaffolds:

- `~/.tidal/config.yaml`
- `~/.tidal/.env`
- `~/.tidal/auction_pricing_policy.yaml`

At minimum, put the API auth values in `~/.tidal/.env` or export them in your shell:

```bash
export TIDAL_API_BASE_URL=https://api.tidal.wavey.info
export TIDAL_API_KEY=<cli-client-api-key>
```

You can also pass `--api-base-url` and `--api-key` per command, but `~/.tidal/.env` is the normal path.

Client-side config values that are often useful in `~/.tidal/config.yaml`:

- `tidal_api_base_url`
- `tidal_api_request_timeout_seconds`
- shared RPC timeout settings if you also do local preview/broadcast work

## Wallet Flags

Broadcasting commands share the same wallet surface:

- `--sender`: address to preview and broadcast from
- `--account`: Foundry keystore name under `~/.foundry/keystores`
- `--keystore`: explicit keystore path
- `--password-file`: file containing the keystore password

The keystore secrets themselves usually live in `~/.tidal/.env`:

- `TXN_KEYSTORE_PATH`
- `TXN_KEYSTORE_PASSPHRASE`

Example:

```bash
tidal kick run \
  --broadcast \
  --sender 0xYourAddress \
  --account wavey3
```

## Read-Only Workflows

Inspect recent kicks:

```bash
tidal logs kicks
tidal logs kicks --status CONFIRMED
tidal logs kicks --source 0xSource
```

Inspect scan history:

```bash
tidal logs scans
```

Inspect one historical run:

```bash
tidal logs show <run_id>
```

Inspect current kick candidates:

```bash
tidal kick inspect
tidal kick inspect --source-type fee-burner
tidal kick inspect --auction 0xAuction
tidal kick inspect --show-all
```

## Kick Workflow

### Dry run

Use this to see candidates in cached order without broadcasting:

```bash
tidal kick run
```

The CLI client flow ranks candidates from cached scanner data, then prepares one candidate at a time. It does not live-quote the whole shortlist up front.

### Broadcast

```bash
tidal kick run \
  --broadcast \
  --sender 0xYourAddress \
  --account wavey3
```

The CLI will:

1. inspect candidates
2. prepare the next exact candidate through the API
3. show a confirmation summary
4. sign locally
5. broadcast locally
6. report broadcast and receipt data back to the API

Useful flags:

- `--limit`: cap how many candidates are considered
- `--source-type`: `strategy` or `fee-burner`
- `--source`: target one source address
- `--auction`: target one auction
- `--bypass-confirmation`: skip interactive confirmation
- `--verbose`: show more diagnostic detail
- `--allow-missing-curve-quote`: relax Curve quote strictness for this run

## Auction Workflows

### Deploy an auction

```bash
tidal auction deploy \
  --want 0xWant \
  --receiver 0xReceiver \
  --starting-price 1234
```

### Enable auction tokens

```bash
tidal auction enable-tokens 0xAuction
tidal auction enable-tokens 0xAuction --extra-token 0xToken
```

### Settle an active auction

```bash
tidal auction settle 0xAuction
tidal auction settle 0xAuction --token 0xActiveToken
tidal auction settle 0xAuction --sweep
```

## Confirmations And Warnings

The kick confirmation view separates:

- auction details: sell amount, quoted output, start/min prices, pricing profile
- send details: sender, gas estimate, gas limit, base fee, max fee

One important warning compares:

- the live quote output amount
- against the evaluated spot output implied by cached sell-token USD value and a just-in-time want-token USD price

The threshold is controlled by `txn_quote_spot_warning_threshold_pct`.

See [Pricing](pricing.md) for the exact formula.

## Failure Modes To Expect

Common CLI client-facing failures:

- `curve quote unavailable`: the fresh quote succeeded overall, but Curve did not provide a usable route and strict Curve mode was enabled
- `below threshold on live balance`: cached shortlist value looked large enough, but the current on-chain balance does not
- `database is locked; retry the request`: the server hit SQLite write contention
- API 401: invalid or missing bearer token

## When To Use `tidal-server` Instead

Use `tidal-server` only when you are operating the server itself:

- database migration
- scan daemon
- kick daemon
- API serving
- API key management

For day-to-day remote execution, use the CLI client `tidal`.

## Command Reference

Use these pages when you need exact command shapes or flag guidance:

- [CLI Client Overview](cli-client-reference.md)
- [CLI Client: `tidal init`](cli-client-init.md)
- [CLI Client: `tidal kick`](cli-client-kick.md)
- [CLI Client: `tidal auction`](cli-client-auction.md)
- [CLI Client: `tidal logs`](cli-client-logs.md)
