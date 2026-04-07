# AuctionKicker Simplification Plan

## Decision

Adopt a resolver-first model:

- `resolveAuction()` is the only on-chain cleanup primitive.
- `kick()` and `batchKick()` only run against auctions that are already clean.
- the operator and scanner surface keeps the verb `settle`.
- default `settle` only closes stuck / stale lots.
- `settle --force` is the explicit override for force-closing a live funded lot.
- backward compatibility is not required; legacy recovery surfaces should be deleted.

This preserves the intended invariant:

- once a lot has been resolved, later auction-global param changes should not revive that old lot
- cleanup happens before a new kick, not during the kick
- regular `kick()` is the only kick primitive that remains

## Contract End State

The long-term `AuctionKicker` surface should be only:

- `kick(...)`
- `batchKick(...)`
- `previewResolveAuction(...)`
- `resolveAuction(..., bool forceLive)`
- `enableTokens(...)`
- owner / keeper admin

Delete these contract surfaces entirely:

- `kickExtended(...)`
- `KickParamsExtended`
- `_kickExtended(...)`
- `_appendSettleCommands(...)`
- `sweepAndSettle(...)`
- `SweepAndSettled`

Remove the remaining legacy cleanup path from regular kick:

- remove `settleToken` from `KickParams`
- remove `settleToken` from `kick(...)`
- remove `settleToken` from `batchKick(...)`
- remove `settleToken` from `Kicked(...)`
- remove any kick-side `settle()` call

Keep `AuctionResolved(...)` as the only closeout event.

## Contract Rules

The contract should own per-lot classification, not the CLI.

Add one preview view that mirrors the resolver state machine exactly:

- `previewResolveAuction(address auction, address sellToken)`

Recommended preview shape:

- `path`
- `active`
- `kickedAt`
- `balance`
- `requiresForce`
- `receiver`

`resolveAuction(auction, sellToken, forceLive)` should use the same internal classifier as `previewResolveAuction(...)`.

Force semantics should be enforced on-chain:

- if the current lot is `active && balance > 0` and `forceLive == false`, revert
- otherwise execute the normal resolver path

This keeps the default “do not close healthy live auctions” invariant race-safe even if state changes after off-chain preview.

The contract should continue to enforce:

- `IAuction(auction).governance() == tradeHandler`
- keeper-or-owner authorization
- `sellToken != address(0)`
- `sellToken != IAuction(auction).want()`

No contract code should remain that stages `settle()` calls between global setter changes.

## Closeout Classifier

Internally, preview and resolve only need these reads per token:

- `isActive(token)`
- `kicked(token)`
- `ERC20(token).balanceOf(auction)`

This is sufficient to distinguish happy-path live auctions from stuck / closeable ones. No closeout decision should depend on `available()`, `price()`, `minimumPrice()`, or `auctionLength()`.

Discovery should be multicall-first:

- batch `getAllEnabledAuctions()` across all auctions being inspected for settlement
- batch `previewResolveAuction(...)` across discovered `(auction, token)` pairs
- batch `isAnActiveAuction()` across candidate auctions only for reporting / kick scheduling hints

The scanner and operator inspection path should use the shared multicall-capable readers that already exist in the runtime. Do not fall back to one-RPC-call-per-token discovery unless multicall is explicitly unavailable.

`isAnActiveAuction()` is an optimization and reporting hint only. It must not be used to skip token enumeration for settlement discovery, because inactive auctions can still contain stale or stranded lots.

Per token, the classifier is:

1. `active && balance > 0`
   live in-progress lot
   default `settle`: noop / skip
   `settle --force`: actionable

2. `active && balance == 0`
   stale sold-out lot
   default `settle`: actionable

3. `!active && kicked != 0 && balance > 0`
   stale kicked lot with stranded inventory
   default `settle`: actionable

4. `!active && kicked != 0 && balance == 0`
   stale kicked empty lot
   default `settle`: actionable

5. `!active && kicked == 0 && balance > 0`
   inactive clean lot with residual inventory
   default `settle`: actionable

6. `!active && kicked == 0 && balance == 0`
   clean lot
   default `settle`: noop

`resolveAuction()` is the executor for all actionable states. The preview view is the canonical source for which path applies and whether force is required.

Ambiguity and failure handling:

- if preview data for a token is missing, inconsistent, or fails to decode, that token is not actionable by default
- auto-settle must fail closed and skip ambiguous tokens or auctions
- explicit operator settle should return an error for the ambiguous target instead of guessing
- multiple live funded lots are treated as an anomaly; they are never implicitly force-closed

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
2. enumerate all blocking / stale lots on that auction
3. use `previewResolveAuction(...)` to classify each lot
4. if any such lots exist, prepare `resolveAuction(..., false)` actions instead of a kick
5. defer the kick candidate until a later run after cleanup
6. only prepare `kick(...)` once the auction is clean

Use this pre-kick policy:

- active lot with sell balance:
  do not auto-force resolve from the transaction service; treat as a live auction blocker and skip / defer the kick
- any other non-clean lot:
  prepare `resolveAuction(...)`
- fully clean auction:
  allow regular `kick(...)`

Live funded lots do not block cleanup of separate stale lots on the same auction. They only block a new kick and any implicit force-close behavior.

This keeps force-unwinding a live funded lot as an explicit operator action, not an automatic kick-side behavior.

## Operator / API / CLI Changes

Keep the operator verb `settle`, but map it to `resolveAuction(...)` internally.

The operator model should be:

- `tidal auction settle AUCTION`
- optional `--token`
- optional `--force`

Recommended semantics:

- without `--token`, settle all default-actionable lots on the auction
- with `--token`, settle only that lot
- with `--token`, inspection should probe the requested token even if it is not returned by enabled-token discovery
- default `settle` closes only the default-actionable stuck states from the classifier above
- `--force` additionally allows closing a live funded lot
- `--force` should require `--token`
- without `--token`, prepare may return multiple `resolveAuction(..., false)` transactions, one per actionable lot
- live funded lots on the same auction do not prevent default settle from closing separate stale lots
- if an auction is only progressing on the happy path, default `settle` should return a clean noop, not an error

Delete these legacy concepts from the operator path:

- settlement method enum: `auto`, `settle`, `sweep_and_settle`
- `--sweep`
- `requestedSweep`
- any `sweep-and-settle` wording in renderer copy

Keep the internal operation name:

- `resolve_auction`

Use `settle` only as the operator-facing verb:

- CLI command stays `tidal auction settle`
- API route stays `POST /api/v1/tidal/auctions/{auction}/settle/prepare`
- control-plane payloads expose `force`, not settlement-method enums
- `--force` maps directly to `forceLive = true` on the contract call

Internally, the operator/API layer should only ever prepare `resolveAuction(..., forceLive)` or return noop / error.

## Scanner Changes

The scanner should use the same classifier as the operator path, with `force = false`.

That means:

- `scan run --auto-settle` becomes “auto-close stuck auctions”
- it should never auto-force a live funded lot
- it should report live funded lots as in progress, not stuck
- it should prepare / send `resolveAuction(..., false)`, not literal `settle(token)`
- if discovery is ambiguous or incomplete, it should fail closed and skip that lot / auction instead of guessing
- if an auction contains both stale lots and separate live funded lots, it should close only the stale lots

The scanner should inspect enabled lot tokens for each auction. An operator can still pass `--token` explicitly for manual closeout of a specific lot.

## Persistence And Types

Simplify runtime types and operation enums:

- remove `sweep_and_settle` from active write paths
- remove `PreparedSweepAndSettle`
- remove `KickRecoveryPlan`
- remove kick preview / payload fields related to kick-time cleanup
- remove `settleToken` from kick prepare types and payloads

For persistence:

- no new prepared actions or kick log rows should ever use `sweep_and_settle`
- new cleanup actions should use only `resolve_auction`

Historical rows may remain readable if that is cheap, but no live code path should continue producing legacy operations.

## Tests

Contract tests:

- remove `kickExtended` coverage
- remove `sweepAndSettle` coverage
- remove kick-side `settleToken` coverage
- add `previewResolveAuction(...)` coverage for each resolver path
- keep and expand `resolveAuction` path coverage
- add `resolveAuction(..., false)` revert coverage for live funded lots
- add `resolveAuction(..., true)` coverage for forced live funded closeout
- add `resolveAuction` guard tests for `want` token and zero token

Transaction service tests:

- assert dirty auctions produce resolver actions, not kick-time recovery plans
- assert kick preparation skips / defers live funded auctions instead of self-healing during kick
- assert plain `kick()` payloads no longer include `settleToken`
- assert multicall preview discovery is used for settlement classification

Operator/API/CLI tests:

- keep `settle` as the command / route name
- remove `--sweep` expectations
- remove settlement-method enum coverage
- assert default `settle` prepares only default-actionable stuck states
- assert `--force` is required for live funded lot resolution
- assert `--force` without `--token` is rejected
- assert default `settle` returns noop on healthy live auctions
- assert explicit `--force` produces `resolveAuction(..., true)`

Scanner tests:

- assert `--auto-settle` prepares / executes `resolveAuction(..., false)`
- assert live funded lots are skipped as in-progress
- assert stale sold-out and stale stranded lots are auto-closeable
- assert preview read failures are skipped fail-closed

## Rollout Sequence

Implement in this order:

1. contract cleanup
2. runtime planner / executor cleanup
3. scanner migration from direct `settle()` to `resolveAuction()`
4. operator/API/CLI simplification around `settle` and `--force`
5. type / audit / payload cleanup
6. docs cleanup
7. deploy new `AuctionKicker` and point config to it

Do not preserve mixed-mode behavior during the refactor. This should be a breaking, internally consistent change set.

## Bottom Line

The clean design is:

- one on-chain cleanup primitive: `resolveAuction()`
- one operator verb: `settle`
- one explicit override: `--force`
- one kick primitive: plain `kick()`
- one default scanner policy: close stuck lots, never force-close live funded lots

That removes the old split-brain design where cleanup logic lived partly in the CLI and partly in the contract, while preserving a simple operator mental model.
