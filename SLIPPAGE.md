# Slippage Guard Plan

## Goal

If we add a slippage guard, keep it small.

It should:

- be token-keyed, not profile-keyed
- be opt-in per token
- run after `usd_kick_limit` sizing
- use the existing live quote call
- avoid extra external API calls

This is a follow-on guard, not a replacement for [`SIZING.md`](SIZING.md).

## Why The Previous Plan Was Wrong

The earlier version was too complex for this codebase because it:

- moved control back to pricing profiles
- added two extra live `/v1/price` calls per guarded kick
- added too much audit and logging machinery for a first cut

That is not the right tradeoff.

## Simple Direction

If we do this at all, the guard should compare:

- the live quote for the already-sized sell amount
- against a cached spot-value estimate for that same size

If the quote is too far below the cached estimate, skip the kick.

## Config

Keep using [`auction_pricing_policy.yaml`](auction_pricing_policy.yaml).

Use one flat token map, similar to `usd_kick_limit`:

```yaml
max_quote_discount_bps:
  "0x04ACaF8D2865c0714F79da09645C13FD2888977f": 1000
```

Meaning:

- for this token, block the kick if the quoted output is more than `1000` bps below the cached spot-implied output
- if a token is not present, no slippage guard is applied

This stays token-keyed and opt-in.

## Data Inputs

Use:

- `selected_sell_raw` from the sizing flow
- the existing live quote from `TokenPriceAggProvider.quote()`
- cached scanner prices, not new live `/v1/price` calls

To make that work cleanly, the txn path needs both:

- cached sell-token USD price
- cached want-token USD price

The sell-token cached price already exists as `candidate.price_usd`.

For the want token, the simplest path is to add cached `want_price_usd` to `KickCandidate` during shortlist construction, rather than fetching another live price during `prepare_kick()`.

## Comparison

After `usd_kick_limit` sizing has selected the final sell amount:

1. Convert `selected_sell_raw` to normalized sell amount.
2. Compute cached spot-implied output:

   `expected_out_at_cached_spot = selected_sell_normalized * sell_price_usd / want_price_usd`

3. Compute quoted output from the live quote result.
4. Compute discount:

   `quote_discount_bps = max(0, (expected_out_at_cached_spot - quoted_out) / expected_out_at_cached_spot * 10_000)`

5. If `quote_discount_bps > max_quote_discount_bps[token]`, skip the kick.

If the quote is better than cached spot, treat the discount as `0`.

## Failure Mode

Fail open.

If any of these are missing or invalid:

- cached sell token price
- cached want token price
- quote amount

do not block the kick. Just skip the slippage check.

That keeps this as a safety guard, not a new source of fragility.

## Enforcement

Blocked kicks should be recorded as:

- `status = SKIP`
- explicit `error_message`

Example:

`blocked by slippage guard: quote 12.34% below cached spot (1234 bps > 1000 bps threshold)`

`SKIP` is the correct behavior because this is expected market protection, not a system failure.

## Persistence

Do not add new DB columns in v1.

If we want audit detail, the smallest acceptable option is:

- keep the human-readable reason in `kick_txs.error_message`
- optionally attach a small `slippageCheck` object inside `quote_response_json`

Do not build a large audit schema for this.

## Code Changes

If implemented, the minimal touchpoints are:

- [`tidal/transaction_service/pricing_policy.py`](tidal/transaction_service/pricing_policy.py)
  - load `max_quote_discount_bps` as a flat token map
- [`tidal/transaction_service/evaluator.py`](tidal/transaction_service/evaluator.py)
  - include cached `want_price_usd` in `KickCandidate`
- [`tidal/transaction_service/types.py`](tidal/transaction_service/types.py)
  - add `want_price_usd` to `KickCandidate`
- [`tidal/transaction_service/kicker.py`](tidal/transaction_service/kicker.py)
  - after sizing and after quote validation, evaluate quote discount for guarded tokens
  - block with `SKIP` when over threshold
- tests
  - guarded token within threshold passes
  - guarded token over threshold skips
  - missing cached want price does not block
  - unguarded token keeps current behavior

## Recommendation

Do not implement this immediately unless we see real cases where `usd_kick_limit` is still not enough.

Order of operations should be:

1. run with `usd_kick_limit`
2. observe real outcomes
3. only add this guard for specific problematic tokens

That keeps the system simple and avoids solving a theoretical problem with a complicated mechanism.
