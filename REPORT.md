# AuctionKicker Simplification Plan

## Decision

Adopt a **resolver-first** model:

- `resolveAuction()` is the only cleanup primitive.
- `kick()` and `batchKick()` only run against auctions that are already clean.
- remove backward compatibility and delete legacy recovery surfaces.

This means your understanding is the intended invariant:

- once a lot has been properly resolved, later auction-global param changes should not revive it
- cleanup should happen before a new kick, not during the kick
- regular `kick()` should be the only kick primitive that remains

## Target End State

The long-term `AuctionKicker` surface should be only:

- `kick(...)`
- `batchKick(...)`
- `resolveAuction(...)`
- `enableTokens(...)`
- owner / keeper admin

Delete these contract surfaces entirely:

- `kickExtended(...)`
- `KickParamsExtended`
- `_kickExtended(...)`
- `_appendSettleCommands(...)`
- `sweepAndSettle(...)`
- `SweepAndSettled`

Keep `AuctionResolved(...)` as the only closeout event.

## Contract Changes

Update `contracts/src/AuctionKicker.sol` to reflect the simplified model:

- remove all `kickExtended` code paths and recovery-planner support
- remove the `sweepAndSettle` wrapper entirely
- keep `resolveAuction()` as the single lot-resolution entrypoint
- keep the existing balance-driven resolver state machine

Add explicit resolver scope guards:

- reject `sellToken == IAuction(auction).want()`
- reject `sellToken == address(0)`

The contract should continue to enforce:

- `IAuction(auction).governance() == tradeHandler`
- keeper-or-owner authorization

No contract code should remain that stages `settle()` calls between global setter changes.

## Runtime And Planner Changes

Remove legacy kick-time recovery from the transaction service.

Delete these runtime concepts:

- `auction_recovery.py`
- `KickRecoveryPlan`
- `PreparedSweepAndSettle`
- `recovery_plan` branches in planner / executor / tx builder
- any `sweep_and_settle` operation generation

Replace the kick flow with a resolver-first precondition:

1. inspect the target auction before preparing a kick
2. if the auction has resolvable stale lots, prepare `resolveAuction(...)` actions instead of a kick
3. defer the kick candidate until a later run after cleanup
4. only prepare `kick(...)` once the auction is clean

Use this policy for pre-kick inspection:

- active lot with sell balance:
  do not auto-force resolve from the transaction service; treat as a live auction blocker and skip/defer the kick
- active lot with zero balance:
  prepare `resolveAuction(...)`
- inactive kicked lot, funded or empty:
  prepare `resolveAuction(...)`
- inactive clean lot with residual sell balance:
  prepare `resolveAuction(...)`
- clean auction:
  allow regular `kick(...)`

This intentionally keeps force-unwinding a live funded lot as an explicit operator action, not an automatic kick-side behavior.

## Operator / API / CLI Changes

Remove legacy settlement naming and modes from the operator surface.

Rename the cleanup action from **settle** to **resolve**:

- CLI command becomes `tidal auction resolve`
- API route becomes `POST /api/v1/tidal/auctions/{auction}/resolve/prepare`
- control-plane client and prepare service use `resolve`, not `settle`

Delete these legacy concepts from the operator path:

- settlement method enum: `auto`, `settle`, `sweep_and_settle`
- `--sweep`
- `requestedSweep`
- any `sweep-and-settle` wording in renderer copy

Replace them with one clean operator model:

- `tidal auction resolve AUCTION`
- optional `--token`
- optional `--force-live` to explicitly allow resolving a live funded lot

Internally, the operator/API layer should only ever prepare `resolveAuction(...)` or return noop / error.

## Persistence And Types

Simplify runtime types and operation enums:

- remove `sweep_and_settle` from active write paths
- remove `PreparedSweepAndSettle`
- remove recovery-plan serialization fields from kick preview payloads
- remove `resolve`-unrelated legacy fields from settlement decision helpers

For persistence:

- no new prepared actions or kick log rows should ever use `sweep_and_settle`
- new cleanup actions should use only `resolve_auction`

Historical rows may remain readable if that is cheap, but no live code path should continue producing legacy operations.

## Tests

Contract tests:

- remove `kickExtended` coverage
- remove `sweepAndSettle` coverage
- keep and expand `resolveAuction` path coverage
- add `resolveAuction` guard tests for `want` token and zero token

Transaction service tests:

- assert dirty auctions produce resolver actions, not kick-time recovery plans
- assert kick preparation skips/defer live funded auctions instead of self-healing during kick
- remove recovery-plan and `PreparedSweepAndSettle` fixtures

Operator/API/CLI tests:

- replace `settle` prepare route and command coverage with `resolve`
- remove `--sweep` expectations
- assert active legacy wording no longer appears in previews or payloads
- assert `force-live` is the only explicit override for live funded lot resolution

## Rollout Sequence

Implement in this order:

1. contract cleanup
2. runtime planner / executor cleanup
3. operator/API/CLI rename to `resolve`
4. type / audit / payload cleanup
5. docs cleanup
6. deploy new `AuctionKicker` and point config to it

Do not try to preserve mixed-mode behavior during the refactor. This change set should intentionally be breaking and internally consistent.

## Bottom Line

There is a real opportunity to simplify further.

The correct simplification is:

- make `resolveAuction()` the required first step for dirty auctions
- stop doing recovery during `kick`
- delete `kickExtended`
- delete `sweepAndSettle`
- delete legacy CLI/API settlement modes

That produces a much cleaner system:

- cleanup is one primitive
- kicking is one primitive
- the contract owns lot resolution
- the CLI stops carrying legacy recovery semantics
