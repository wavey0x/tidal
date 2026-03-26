# Auction Pricing And Settlement Plan

## Pricing policy

- Keep pricing policy in a dedicated root file: `auction_pricing_policy.yaml`.
- Use a minimal schema:
  - `default_profile`
  - `profiles`
  - `auctions`
- Treat all unlisted `(auction, sell_token)` pairs as `volatile`.
- Use explicit overrides only for non-default cases such as stable or correlated pairs.

## Profiles

- `volatile`
  - `start_price_buffer_bps: 1000`
  - `min_price_buffer_bps: 500`
  - `step_decay_rate_bps: 50`
- `stable`
  - `start_price_buffer_bps: 100`
  - `min_price_buffer_bps: 50`
  - `step_decay_rate_bps: 1`

## Contract changes

- Extend `contracts/src/AuctionKicker.sol` to support per-kick:
  - `stepDecayRateBps`
  - `settleToken`
- Add `sweepAndSettle(address auction, address sellToken)` for keeper or owner use.
- `sweepAndSettle()` must:
  - call `auction.sweep(sellToken)`
  - transfer swept tokens from `TradeHandler` to `auction.receiver()`
  - call `auction.settle(sellToken)`
- Allow constructor-time keeper seeding via an address array.

## Transaction service

- Resolve pricing profile in the transaction service, not onchain.
- Use multicall-backed auction inspection for:
  - `isAnActiveAuction()`
  - `isActive(source)`
  - `available(source)`
  - `price(source)`
  - `minimumPrice()`
- Prepare three operation types:
  - normal kick
  - kick with pre-kick settle of a sold-out active lot
  - `sweep_and_settle` for auctions stuck at or below `minimumPrice`
- Detect stuck auctions using the auction’s own `price(address)` view.
- Fail closed if required live inspection data is missing.

## Logging and surfaces

- Persist and expose:
  - `step_decay_rate_bps`
  - `settle_token`
  - `stuck_abort_reason`
- Show `sweep_and_settle` entries in the Log tab/table.
- Show the resolved pricing profile during manual confirmation.

## Ops tooling

- Provide a helper script in `scripts/sweep_and_settle.py`.
- Seed the default deploy keeper in `contracts/script/DeployAuctionKicker.s.sol`.
