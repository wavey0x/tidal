# Slippage Guard Plan

## Goal

Block kicks when the live quote is materially worse than a live spot-value estimate for the same sell size.

This should be enforced in the transaction service, not onchain.

## Config shape

Put the config in `auction_pricing_policy.yaml`, inside each pricing profile.

Recommended shape:

```yaml
default_profile: volatile

profiles:
  volatile:
    start_price_buffer_bps: 1000
    min_price_buffer_bps: 500
    step_decay_rate_bps: 50
    max_price_impact_bps: 1500

  stable:
    start_price_buffer_bps: 100
    min_price_buffer_bps: 50
    step_decay_rate_bps: 1
    max_price_impact_bps: 100
```

Why this is the best fit:

- slippage tolerance is naturally profile-level, just like buffers and decay
- we already classify `(auction, sell_token)` into a profile
- no extra override map is needed
- if a pair needs looser or tighter handling later, add a new profile instead of inventing pair-specific slippage config

Behavior:

- omitted or `null` `max_price_impact_bps` means slippage blocking is disabled for that profile
- manual `auctions` overrides keep working exactly as they do now

## Comparison method

Use a live quote and live spot prices from the same API family during `prepare_kick()`.

Do not use the scanner-cached `candidate.price_usd` for enforcement.

Algorithm:

1. Resolve the pricing profile for `(auction, sell_token)`.
2. Get the sell quote exactly as we do today.
3. After the quote passes existing gating, fetch live USD spot prices for:
   - `sell_token`
   - `want_token`
4. Compute the spot-implied output for the exact quoted sell size:

   `expected_out_at_spot = sell_amount_normalized * sell_token_usd / want_token_usd`

5. Compute price impact:

   `price_impact_bps = max(0, (expected_out_at_spot - quoted_out) / expected_out_at_spot * 10_000)`

6. If `price_impact_bps > max_price_impact_bps`, block the kick.
7. If the quote is better than spot, treat impact as `0`.

This is the cleanest implementation because it compares like-for-like:

- exact sell size
- exact token pair
- live quote vs live spot

## False-positive controls

To keep findings accurate and avoid noisy blocking:

- Use live `/v1/price` results from `TokenPriceAggProvider`, not cached DB prices.
- Fetch both spot prices during the same prepare cycle.
- Fail open if either spot price is missing, zero, malformed, or the API call fails.
- Keep the existing `require_curve_quote` gate ahead of the slippage check.
- Compare against the quoted sell size, not a unit price.
- Keep thresholds on profiles so we can create a dedicated profile for problematic pairs instead of sprinkling exceptions through the config.

V1 should not try to infer provider quality beyond this.

If we later need even fewer false positives, the next clean extension is:

- only enforce when the price API exposes a narrow enough provider spread
- otherwise log `slippage check skipped: low confidence`

That should be a future refinement, not part of the first cut.

## Enforcement behavior

Blocked kicks should be recorded as normal kick attempts with:

- `status = ERROR`
- a very explicit `error_message`

Recommended message format:

`blocked by slippage guard: quote 12.34% below spot (1234 bps > 800 bps threshold)`

This is better than a generic skip because:

- it is operationally obvious
- it shows up clearly in existing log surfaces
- it preserves the fact that the pair was considered but rejected

## Log detail

Do not add new DB columns in v1.

Instead:

- keep the human-readable reason in `kick_txs.error_message`
- attach a `slippageCheck` object inside `quote_response_json`

Recommended `slippageCheck` payload:

```json
{
  "evaluated": true,
  "blocked": true,
  "profile": "stable",
  "thresholdBps": 100,
  "priceImpactBps": 184,
  "sellTokenPriceUsd": "0.9998",
  "wantTokenPriceUsd": "1.0001",
  "expectedOutAtSpot": "999.70",
  "quotedOut": "981.30",
  "reason": "quote_below_spot_threshold"
}
```

Also add structured logger events:

- `txn_slippage_check_passed`
- `txn_slippage_check_skipped`
- `txn_slippage_check_blocked`

Each should include:

- source
- auction
- sell token
- want token
- profile
- threshold bps
- computed impact bps
- spot-implied output
- quoted output
- quote request URL when available

## Code changes

Minimal implementation surface:

- `tidal/transaction_service/pricing_policy.py`
  - add `max_price_impact_bps` to `AuctionPricingProfile`
  - load and validate the new optional field
- `tidal/pricing/token_price_agg.py`
  - reuse `quote_usd()` for live sell/want spot prices
- `tidal/transaction_service/kicker.py`
  - add a helper to fetch sell/want spot prices concurrently
  - add a helper to evaluate slippage and return structured audit data
  - enforce the check after quote validation and before building `PreparedKick`
  - persist the slippage audit object in `quote_response_json`
- tests
  - profile parsing
  - quote better than spot
  - quote within threshold
  - quote above threshold blocks
  - missing spot price does not block

## Rollout

Best rollout:

1. implement the guard
2. start with conservative thresholds
3. watch blocked events in logs
4. tighten only after reviewing real blocked examples

Recommended initial values:

- `volatile.max_price_impact_bps = 1500`
- `stable.max_price_impact_bps = 100`

Those are starting points, not hard rules.
