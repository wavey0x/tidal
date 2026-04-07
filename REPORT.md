# AuctionKicker Audit Report

## Summary

`resolveAuction()` is the right new primitive.

It materially improves the design by moving lot-resolution policy into `AuctionKicker` and out of the operator/API layer. That is a real simplification, and it aligns with the intent of `REFACTOR.md`.

However, the recent changes do **not** make `kickExtended()` obsolete by themselves.

That is because `resolveAuction()` and `kickExtended()` solve different problems:

- `resolveAuction()` solves **closeout / cleanup** of an existing lot.
- `kickExtended()` solves **kick-time recovery** when changing auction-global params would revive stale empty lots and block later setters.

So the right conclusion is:

- the resolver design is good
- `sweepAndSettle()` is now legacy compatibility surface
- `kickExtended()` is not dead yet, but it has become a **transitional** primitive rather than a desirable long-term one

## What The Current Primitives Really Are

### 1. `resolveAuction()`

This is now the authoritative cleanup primitive.

It reads lot state on-chain and handles:

- active + funded -> sweep + settle
- active + empty -> settle
- inactive kicked + funded -> sweep + reset
- inactive kicked + empty -> reset
- inactive clean + funded -> sweep
- inactive clean + empty -> no-op

This is the correct direction. The operator CLI no longer needs to model auction lifecycle quirks itself.

### 2. `sweepAndSettle()`

This is no longer a primitive. It is a compatibility alias.

Today it just:

- calls `_resolveAuction()`
- emits the legacy `SweepAndSettled` event only for one path
- emits `AuctionResolved`

That means `sweepAndSettle()` no longer represents a distinct behavior. It only exists because older runtime code still builds `sweepAndSettle(...)` calldata.

### 3. `kickExtended()`

This is still a real primitive, but it is solving a different class of problem.

The current transaction service still uses:

- `auction_recovery.py`
- `KickRecoveryPlan`
- `kickExtended(...)`

for this scenario:

1. prepare a new kick
2. estimate the standard `kick(...)`
3. if the auction-global param changes would cause stale empty lots to become active and block later setters, build a staged recovery plan
4. execute `kickExtended(...)` with interleaved `settle()` calls between setters

So `kickExtended()` is currently a **kick-time self-healing mechanism for global setter sequencing**.

That is separate from `resolveAuction()`.

## Design Assessment

### The good news

The resolver work gave the contract the correct cleanup primitive.

That means cleanup is now:

- balance-driven
- on-chain
- atomic
- owned by the mech

This is a much better architecture than the old CLI-heavy settlement logic.

### The remaining complexity

The contract still carries two overlapping generations of recovery logic:

- new generation: `resolveAuction()`
- old generation: `kickExtended()` + off-chain recovery planning

That overlap is where most remaining complexity lives.

The important point is that this overlap is not inside `resolveAuction()`. It is in the **kick path**.

### The key question

Do you want kicks to remain self-healing?

If yes:

- keep `kickExtended()`
- accept that the contract still has two kinds of lifecycle logic

If no:

- make `resolveAuction()` the only cleanup primitive
- require auctions to be cleaned before new kicks
- remove the kick-time recovery planner and eventually remove `kickExtended()`

## Recommendations

### 1. Treat `resolveAuction()` as the permanent cleanup primitive

This part looks right and should stay.

I would keep the state-machine behavior in the contract exactly where it is now.

### 2. Treat `sweepAndSettle()` as deprecated

This is the cleanest near-term cleanup opportunity.

Recommendation:

- stop calling `sweepAndSettle()` anywhere in runtime code
- migrate legacy transaction-service sweep/settle execution to `resolveAuction()`
- keep the Solidity wrapper temporarily for deployment compatibility and event continuity
- remove it in a later contract revision once no callers depend on it

So: `sweepAndSettle()` should be considered dead **conceptually**, even if it remains temporarily for compatibility.

### 3. Keep `kickExtended()` for now, but demote it to transitional status

I do **not** think it should be removed immediately.

Reason:

- current kick execution still mutates auction-global params
- the runtime still depends on kick-time recovery planning
- removing `kickExtended()` today would require a coordinated refactor of the transaction service and a new invariant around pre-kick cleanup

But I also do **not** think `kickExtended()` should be treated as part of the final clean design.

Recommendation:

- keep it for the next deployment
- explicitly document it as transitional
- plan a follow-up refactor where kicks assume auctions are already clean

That follow-up would let you remove:

- `kickExtended(...)`
- `KickRecoveryPlan`
- `auction_recovery.py`
- kick-time staged settle planning in the transaction service

### 4. Make the primitive boundary explicit in `resolveAuction()`

The current design intent is "resolve one sell-token lot in an auction", not "general-purpose sweep any token held by the auction".

I think the contract should enforce that more explicitly.

Recommendation:

- add a guard that rejects `sellToken == IAuction(auction).want()`

Why:

- it keeps `resolveAuction()` scoped to lot management
- it prevents the primitive from quietly becoming a generic arbitrary-token rescue function
- it preserves room for a future separate rescue primitive if you ever want one

This matches the cleaner design direction.

### 5. Simplify the long-term public surface

The clean end-state contract surface should be:

- `kick(...)`
- `batchKick(...)`
- `resolveAuction(...)`
- `enableTokens(...)`
- owner / keeper admin

Everything else is either transitional or compatibility baggage.

Concretely:

- `sweepAndSettle()` -> compatibility wrapper, then remove
- `kickExtended()` -> transitional self-healing kick path, then remove if resolver-first cleanup becomes policy

## Bottom Line

The recent changes were directionally correct.

You **do** now have the right primitive to move settlement/cleanup complexity out of the CLI and into the contract: `resolveAuction()`.

But that does **not** automatically mean `kickExtended()` is dead, because `kickExtended()` is solving a different remaining problem in the kick path.

So the best design judgment is:

- `resolveAuction()` is a keeper
- `sweepAndSettle()` is legacy
- `kickExtended()` is transitional

If the goal is a truly simpler `AuctionKicker`, the next step is not to add more resolver logic. It is to migrate the runtime toward a **resolver-first cleanup model**, then remove kick-time recovery planning once that invariant is real.
