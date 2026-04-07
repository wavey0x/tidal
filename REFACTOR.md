# Auction Lot Resolver Refactor

## Thesis

The closeout primitive in `contracts/src/AuctionKicker.sol` should do one thing well:

- get sell tokens out of the auction immediately
- settle when settlement is actually valid
- locally reset stale inactive lot state when needed

The CLI should stop trying to infer recovery plans from `available()` and auction-global parameter math. That logic belongs in the mech, and most of it should disappear.

## Problem We Are Actually Solving

The operational problem is not "make every lot perfectly settleable under every pricing configuration."

The real problem is simpler:

- sometimes the auction still holds sell tokens
- the keeper needs a way to retrieve them immediately
- the current `sweepAndSettle(...)` path fails because it relies on `available(sellToken)`

That is the wrong primitive. `available()` is intentionally price-gated. When a lot is inactive, `available()` returns `0` even if the auction still holds real token balance.

So the resolver should be balance-driven, not `available()`-driven.

## Verified Contract Facts

From the verified auction source:

- `isActive(_from)` is price-driven, not balance-driven.
- `available(_from)` returns `0` whenever `isActive(_from) == false`.
- `settle(_from)` requires both:
  - `isActive(_from) == true`
  - `ERC20(_from).balanceOf(address(this)) == 0`
- `sweep(_token)` transfers the full token balance to `msg.sender`, not to `receiver`.
- `disable(_from)` and `enable(_from)` are both per-token operations with `onlyGovernance`, and neither is blocked by `isAnActiveAuction()`.
- `disable(_from)` deletes the lot struct and removes the token from `enabledAuctions`.
- `enable(_from)` recreates the lot struct, recomputes scaler, restores relayer approval, and re-adds the token.

Those facts imply two important design conclusions:

- retrieval should be based on `ERC20(sellToken).balanceOf(auction)`, not on `available()`
- stale inactive state can be reset locally with `disable -> enable`, without touching auction-global pricing params

## Design Goal

We want one keeper-facing closeout function in `AuctionKicker` that is:

- simple
- idempotent
- safe under weird auction state
- able to clear tokens from both live and non-live auctions

The resolver should not need:

- global setter mutation
- restore logic
- off-chain recovery planning
- CLI flags for pricing rescue behavior

## Preferred API Shape

Avoid function sprawl.

The cleanest option is to keep one public closeout primitive and broaden its behavior:

```solidity
function sweepAndSettle(address auction, address sellToken) external onlyKeeperOrOwner;
```

The name is slightly narrow, but reusing it keeps the public surface small. Its behavior should become:

- sweep if tokens exist
- settle if settlement is valid
- reset stale inactive lot state if needed
- no-op if there is nothing to do

If we later decide the name is too misleading, renaming it to `resolveLot(...)` is fine, but the design does not require another public entrypoint.

## Required Interface Additions

This revised design needs far less interface expansion than the previous draft.

`contracts/src/interfaces/IAuction.sol` only needs:

- `function kicked(address _from) external view returns (uint256);`
- `function disable(address _from) external;`

Everything else the resolver needs already exists:

- `receiver()`
- `isActive(address)`
- `settle(address)`
- `sweep(address)`
- `enable(address)`

## Resolver Algorithm

The resolver should inspect:

- `auctionBalance = IERC20(sellToken).balanceOf(auction)`
- `active = IAuction(auction).isActive(sellToken)`
- `kickedAt = IAuction(auction).kicked(sellToken)`
- `receiver = IAuction(auction).receiver()`

Then branch as follows.

### Branch 1: Active Lot With Balance

Conditions:

- `active == true`
- `auctionBalance > 0`

Resolution:

1. `sweep(sellToken)` from auction to `TradeHandler`
2. `sellToken.transfer(receiver, auctionBalance)` from `TradeHandler`
3. `settle(sellToken)`

This handles the normal live-lot recovery path.

### Branch 2: Active Lot With Zero Balance

Conditions:

- `active == true`
- `auctionBalance == 0`

Resolution:

1. `settle(sellToken)`

This handles the "active but empty" state directly. No special recovery logic is needed.

### Branch 3: Inactive Lot With Stale Kick State

Conditions:

- `active == false`
- `kickedAt != 0`

Resolution:

1. If `auctionBalance > 0`:
   - `sweep(sellToken)`
   - `sellToken.transfer(receiver, auctionBalance)`
2. `disable(sellToken)`
3. `enable(sellToken)`

This is the local reset path. It solves the genuinely hard inactive-empty case without touching auction-global params.

It also works for inactive lots that still hold inventory: recover the tokens first, then reset the stale lot state.

### Branch 4: Inactive Lot Already In Clean State

Conditions:

- `active == false`
- `kickedAt == 0`

Resolution:

1. If `auctionBalance > 0`:
   - `sweep(sellToken)`
   - `sellToken.transfer(receiver, auctionBalance)`
2. Otherwise no-op

If `kickedAt == 0`, there is no stale kick state to clean up. Once any balance is removed, the lot is already clean enough for future kicks.

## State Table

The full state space collapses cleanly under this model.

| State | Conditions | Resolution |
|---|---|---|
| 1. Active, balance > 0 | `isActive == true`, balance `> 0` | Sweep to `TradeHandler`, transfer to receiver, settle |
| 2. Active, balance == 0 | `isActive == true`, balance `== 0` | Settle |
| 3. Inactive, unexpired, balance > 0 | `isActive == false`, `kicked != 0`, balance `> 0` | Sweep to `TradeHandler`, transfer to receiver, disable, enable |
| 4. Inactive, unexpired, balance == 0 | `isActive == false`, `kicked != 0`, balance `== 0` | Disable, enable |
| 5. Inactive, expired, balance > 0 | `isActive == false`, `kicked != 0`, balance `> 0` | Sweep to `TradeHandler`, transfer to receiver, disable, enable |
| 6. Inactive, expired, balance == 0 | `isActive == false`, `kicked != 0`, balance `== 0` | Disable, enable |
| 7. Never kicked or already clean, balance > 0 | `kicked == 0`, balance `> 0` | Sweep to `TradeHandler`, transfer to receiver |
| 8. Never kicked or already clean, balance == 0 | `kicked == 0`, balance `== 0` | No-op |

The important simplification is that the resolver does not need to care why an inactive kicked lot is inactive. Below-floor and expired lots share the same local reset path.

## Why State 4 Is Solved This Way

State 4 is:

- inactive
- still within auction duration or otherwise not formally settled
- empty
- stuck with `kicked != 0`

This state cannot be settled under the current auction rules, and there are no tokens left to re-kick.

`disable -> enable` is the right answer because:

- it is local to the token
- it has no `isAnActiveAuction()` guard
- it resets `kicked` to `0`
- it restores the token to a fresh enabled state
- it does not require any pricing assumptions

This is materially better than pretending State 4 should be a no-op if we care about leaving the lot in a clean reusable state inside the same transaction.

## Why We Should Not Use Global Setter Rescue

The earlier rescue-profile idea should be dropped from the refactor.

Reasons:

- every setter is blocked by `require(!isAnActiveAuction(), "active auction")`
- interleaving setter changes with settles is exactly the complexity we are trying to remove
- restore logic is as hard as activation logic
- the current `kickExtended(...)` and `auction_recovery.py` already demonstrate how awkward this becomes

That is the wrong tool for lot closeout.

## Why I Am Not Making `forceKick` The Default Inactive-Lot Path

`forceKick` is attractive, but it is not the best default for this resolver.

Reasons:

- `forceKick` still inherits the auction's current pricing globals
- a freshly kicked lot is not guaranteed to be active if the fresh price is still below `minimumPrice`
- it fails entirely on empty lots, so you still need another State 4 solution
- once `disable -> enable` exists, it is the more general stale-state reset primitive

`forceKick` may still be useful later for a separate relist-oriented helper, but it is not needed for the closeout primitive we are designing here.

## Why This Design Is Better

This design is better because it uses the actual problem boundary:

- if there are tokens, recover them
- if the lot is settleable, settle it
- if the lot is stale and inactive, reset it locally
- if the lot is already clean, do nothing

It avoids:

- auction-global mutation
- restore sequencing
- pricing math in the CLI
- brittle recovery-plan generation for closeout

## Event Shape

The resolver should emit one event that records which path ran.

Example:

```solidity
event LotResolved(
    address indexed auction,
    address indexed sellToken,
    uint8 path,
    address receiver,
    uint256 recoveredBalance
);
```

Suggested paths:

- `0`: no-op
- `1`: settle only
- `2`: sweep only
- `3`: sweep and settle
- `4`: reset only
- `5`: sweep and reset

## CLI And API Direction

The CLI should become a thin wrapper around this resolver.

It should prepare and preview only these operator outcomes:

- sweep and settle
- settle only
- sweep and reset
- reset only
- sweep only
- no-op

The CLI should not:

- inspect `available()` to decide recoverability
- model pricing rescue behavior
- generate `settleAfterStart` / `settleAfterMin` / `settleAfterDecay` style closeout plans

`auction_recovery.py` may still exist for kick-time behavior if needed, but it should not be part of the lot-closeout path.

## Recommendation

Implement the minimal balance-driven resolver first.

The right closeout primitive is:

- balance-driven
- settlement-aware
- locally resettable via `disable -> enable`
- free of global param mutation

That directly solves the current stuck-auction problem, cleanly resolves State 4, and meaningfully reduces CLI complexity without adding new function sprawl.
