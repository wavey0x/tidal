# Pricing

## Overview

Tidal uses two different kinds of pricing data:

1. cached USD prices from the scanner, used for ranking and sizing
2. just-in-time quotes and want-token pricing, used for transaction preparation and confirmation warnings

The system is intentionally designed so cached prices are good enough for ordering, while live quotes are only fetched when a specific candidate is actually being prepared.

## Scan-Time USD Price Refresh

During each scan, Tidal refreshes token USD prices through:

```text
GET /v1/price
```

Parameters:

- `token`
- `chain_id`
- `use_underlying=true`

The scanner persists:

- `summary.high_price` as the token USD value
- `token.logo_url` as the token logo when present

This cached USD price is used for:

- dashboard USD values
- shortlist ranking
- threshold filtering
- sell-side USD sizing

## Live Quote Path

When a kick candidate is prepared, Tidal fetches a direct token-to-token quote through:

```text
GET /v1/quote
```

Parameters:

- `token_in`
- `token_out`
- `amount_in`
- `chain_id`
- `use_underlying=true`
- `timeout_ms=7000`

Tidal uses:

- `summary.high_amount_out` as the quote output amount
- provider-level statuses and amounts for diagnostics

If the first `200` response has provider failures and no amount, Tidal performs one short soft retry.

## Curve Quote Strictness

By default, kick preparation requires Curve to provide a positive route amount.

That behavior is controlled by:

- config: `txn_require_curve_quote`
- per-run override: `--require-curve-quote` / `--allow-missing-curve-quote`

If strict mode is on and Curve has no usable quote, prepare fails with:

```text
curve quote unavailable (status: ...)
```

## Auction Pricing Profiles

Pricing profiles come from the server's `kick:` section in `config/server.yaml`.

For API-backed `tidal` workflows, that means the tracked server config on the runtime preparing the action, not the local workstation copy.

If a confirmation panel shows an unexpected decay or profile, check the server runtime first.

Each profile defines:

- `start_price_buffer_bps`
- `min_price_buffer_bps`
- `step_decay_rate_bps`

Example intent:

- `volatile`: wider buffers, faster decay
- `stable`: tighter buffers, slower decay

Use `profile_overrides` to pin a specific `(auction, sell token)` pair to a profile:

```yaml
profile_overrides:
  - auction: "0xAuction"
    token: "0xSellToken"
    profile: stable
```

## Sell Sizing

Sell sizing uses:

- the live on-chain source balance
- the cached sell-token USD price
- optional per-token `usd_kick_limit`

That means Tidal can cap a large position to a smaller USD amount without fetching any extra live pricing data first.

## Just-In-Time Want Price

After the live token-to-token quote is computed, Tidal also requests a just-in-time USD price for the want token through:

```text
GET /v1/price
```

This uses the same `summary.high_price` extraction path as the scanner.

That want-token USD mark is not used to rank candidates. It exists to make the confirmation output and warning logic more informative.

## Confirmation Warning

The quote warning compares:

- `live quoted output`
- against `evaluated spot output`

The evaluated spot output is:

```text
cached sell USD value / just-in-time want USD price
```

The live quoted output is:

```text
summary.high_amount_out` from `/v1/quote`
```

If the deviation exceeds `txn_quote_spot_warning_threshold_pct`, the confirmation screen warns that the live quote is higher or lower than evaluated spot.

This is a diagnostic warning, not an automatic rejection.

## What Prices Are Used Where

| Use case | Price source |
|---|---|
| Dashboard USD values | cached `/v1/price` high price |
| Kick shortlist ranking | cached sell-token USD price |
| Threshold filtering | cached sell-token USD price |
| Sell-size cap math | cached sell-token USD price |
| Start/min price calculation | live `/v1/quote` high amount out |
| Confirmation mismatch warning | cached sell USD plus JIT want USD plus live `/v1/quote` |

## Operational Guidance

- Do not use live quote data to rank the shortlist.
- Do not assume the warning means the on-chain transaction is wrong.
- Treat large warning deviations as a signal to inspect the want-token USD mark or quote path.
- Stable-looking pairs should usually be mapped to the `stable` pricing profile in the authoritative server config.
