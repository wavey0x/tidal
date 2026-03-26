# Auction Sizing Plan

## Goal

Prevent oversized kicks for specific tokens without adding more quote API traffic.

The rule is simple:

- if a token has a USD cap configured, size the kick from that cap
- if a token has no USD cap configured, keep the current full-balance behavior
- use the scanner-cached price for sizing
- make exactly one quote call for the final selected amount

## Config

Keep using [`auction_pricing_policy.yaml`](auction_pricing_policy.yaml).

Add one new top-level section:

```yaml
usd_kick_limit:
  "0x04acaf8d2865c0714f79da09645c13fd2888977f": 5000

  "0xD533a949740bb3306d119CC777fa900bA034cd52": 25000
```

This is token-specific only. No defaults. No balance fraction. No profile-based sizing.

## Runtime Behavior

Current behavior in [`tidal/transaction_service/kicker.py`](tidal/transaction_service/kicker.py):

```python
sell_amount = live_balance_raw
```

New behavior:

1. Read `live_balance_raw`.
2. Compute full live USD value from cached `candidate.price_usd`.
3. If full live USD value is below `txn_usd_threshold`, skip as today.
4. Check `usd_kick_limit` for `candidate.token_address`.
5. If there is no token cap, use full balance as today.
6. If there is a token cap, convert that USD cap into a raw token amount.
7. Set:

```text
selected_sell_raw = min(live_balance_raw, usd_cap_raw)
```

8. Compute selected USD value from cached `candidate.price_usd`.
9. If selected USD value is below `txn_usd_threshold`, skip.
10. Quote `selected_sell_raw` once.
11. Build auction pricing from that quote.

Important: do not quote the full balance first.

## Code Changes

### [`tidal/transaction_service/pricing_policy.py`](tidal/transaction_service/pricing_policy.py)

Add token sizing parsing from the same YAML file:

```python
@dataclass(frozen=True, slots=True)
class TokenSizingPolicy:
    token_overrides: dict[str, Decimal]

    def resolve(self, token_address: str) -> Decimal | None: ...
```

Add:

```python
def load_token_sizing_policy(policy_path: Path | None = None) -> TokenSizingPolicy:
    ...
```

### [`tidal/runtime.py`](tidal/runtime.py)

Load the token sizing policy and pass it into `AuctionKicker`.

### [`tidal/transaction_service/kicker.py`](tidal/transaction_service/kicker.py)

Add `token_sizing_policy` to `AuctionKicker`.

Replace the hardcoded full-balance sizing with:

```python
selected_sell_raw = live_balance_raw
usd_cap = self.token_sizing_policy.resolve(candidate.token_address) if self.token_sizing_policy else None
if usd_cap is not None:
    usd_cap_raw = ...
    selected_sell_raw = min(live_balance_raw, usd_cap_raw)
```

Then:

- compute selected normalized amount
- compute selected USD value from cached price
- skip if selected USD is below threshold
- quote `selected_sell_raw`

## Persistence

Do not add schema changes in v1.

Persist the selected amount in existing `kick_txs` fields:

- `sell_amount`: selected raw amount
- `normalized_balance`: selected normalized amount
- `usd_value`: selected USD estimate

That makes kick logs reflect what was actually sent to auction.

## Tests

Add or update unit tests in [`tests/unit/test_txn_kicker.py`](tests/unit/test_txn_kicker.py):

1. token-specific USD cap reduces the sell amount
2. token missing from `usd_kick_limit` keeps full-balance behavior
3. capped amount below threshold returns `SKIP`
4. quote API is called once with the selected amount
5. persisted `sell_amount` / `normalized_balance` / `usd_value` reflect the selected amount

## Deferred

Do not include these in v1:

- default token caps
- balance-fraction caps
- quote ladders
- impact models
- run-level caps
- schema changes for full-balance audit fields

## Summary

This should stay a small change:

- one new `usd_kick_limit` section in the existing YAML
- one small loader
- one sizing branch in `AuctionKicker`
- no extra quote calls
- no migration
