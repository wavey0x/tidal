# Auction Recovery Plan

## Goal

Unblock kicks on shared auctions without changing `Auction.sol`, by extending `AuctionKicker` and Tidal's prepare flow to detect and clear stale empty lots that would be revived by global parameter updates.

## Important Clarification

The auction contract does not enforce "only one token can be active at a time".

- `isAnActiveAuction()` returns true if **any** enabled token is active.
- `_kick(_from)` only checks `!isActive(_from)`, not `!isAnActiveAuction()`.
- The pricing setters (`setStartingPrice`, `setMinimumPrice`, `setStepDecayRate`) check `!isAnActiveAuction()`.

That means the recovery planner must evaluate **all enabled tokens** after each staged parameter update and clear **every** empty token that becomes active at that stage.

Relevant source:
- [contracts/src/AuctionKicker.sol](contracts/src/AuctionKicker.sol)
- `Auction.sol` verified source on Etherscan for `0xA00E6b35C23442fa9D5149Cba5dd94623fFE6693`

## Root Cause

`Auction.sol` has global pricing params shared across all enabled tokens on an auction.

An empty lot can remain in kicked state:

- `balance == 0`
- `kicked != 0`
- `isActive == false`

Later, a higher `startingPrice` or other global param update for a different token can make that stale lot active again. Once that happens, the next pricing setter reverts with `active auction`.

This is exactly what happens for:

- auction: `0xA00E6b35C23442fa9D5149Cba5dd94623fFE6693`
- new candidate: `opASF`
- stale empty blocker: `ynRWAx`

Trace result:

1. `transferFrom(opASF)` succeeds
2. `setStartingPrice(opASF)` succeeds
3. `setMinimumPrice(opASF)` reverts `active auction`

## Workaround Strategy

Do not store recovery state on-chain.

Instead:

1. Tidal first tries the existing happy path unchanged.
2. If the normal prepare / gas estimate succeeds, use it.
3. If the normal prepare fails specifically with `active auction`, Tidal runs the recovery planner off-chain.
4. After each staged param update, it evaluates `isActive()` for all enabled tokens.
5. For every token that would become active and has `balance == 0`, it schedules `settle(token)` immediately after that stage.
6. A new extended `AuctionKicker` path executes those staged settles in the same Weiroll sequence.

This requires:

- a new `AuctionKicker` deployment
- no `Auction.sol` upgrade
- no contract storage changes
- no forking or state overrides

## Contract Changes

### 1. Keep the existing happy path untouched

Do not add recovery complexity to the current `KickParams`.

Keep:

- `kick(...)`
- `batchKick(KickParams[] calldata kicks)`
- existing `KickParams`

exactly as they are today for the normal fast path.

### 2. Add a separate extended path

Add a new struct and a single-candidate entrypoint:

```solidity
struct KickParamsExtended {
    address source;
    address auction;
    address sellToken;
    uint256 sellAmount;
    address wantToken;
    uint256 startingPrice;
    uint256 minimumPrice;
    uint256 stepDecayRateBps;
    address settleToken;
    address[] settleAfterStart;
    address[] settleAfterMin;
    address[] settleAfterDecay;
}

function kickExtended(KickParamsExtended calldata p) external;
```

Why a separate type/path is better:

- keeps the common case simple
- avoids bloating the default calldata and command builder
- makes the recovery path explicit in audits and previews
- lets Tidal fall back to recovery only when needed

Why only a single-candidate extended path in v1:

- the current problem is one candidate on one auction
- batching recovery adds complexity quickly
- the standard path still covers normal multi-kick batches
- we can add `batchKickExtended` later if real usage demands it

### 3. Extended-path execution order

Current order:

1. optional `settle`
2. `transferFrom`
3. `setStartingPrice`
4. `setMinimumPrice`
5. `setStepDecayRate`
6. `kick`

Proposed order:

1. optional `settleToken`
2. `setStartingPrice`
3. `settleAfterStart[]`
4. `setMinimumPrice`
5. `settleAfterMin[]`
6. `setStepDecayRate`
7. `settleAfterDecay[]`
8. `transferFrom`
9. `kick`

Why:

- revivals are caused by global param updates
- the existing pre-settle path still needs to work for already-active sold-out lots
- recovery must happen after the stage that revives the stale lot
- transfer should happen last so tokens are not moved before a later revert

### 4. Keep contract stateless

All recovery instructions should come from calldata.

No contract storage should be added.

## Off-Chain Recovery Planner

Add a new planner module in Tidal, for example:

- `tidal/transaction_service/auction_recovery.py`

### When it runs

Only run the recovery planner if the standard prepare / gas estimate fails with `active auction`.

That means:

1. Tidal prepares the normal kick first.
2. If normal gas estimation succeeds, stop there.
3. If it fails with the known `active auction` pattern, build an extended recovery plan and try `kickExtended`.

This keeps the normal path fast and avoids doing extra work on every kick.

### Inputs

- candidate auction
- candidate token
- proposed `startingPrice`
- proposed `minimumPrice`
- proposed `stepDecayRate`

### Reads required

For the auction:

- `getAllEnabledAuctions()`
- `auctionLength()`
- `startingPrice()`
- `minimumPrice()`
- `stepDecayRate()`
- `stepDuration()`

For each enabled token:

- `auctions(token)` / `kicked(token)` data
- auction token balance
- optionally current `isActive(token)` for parity/debug

### Candidate blocker rules

Only consider tokens where:

- same auction
- `kicked != 0`
- `now < kicked + auctionLength`
- auction balance `== 0`

### Simulation stages

For every enabled token, simulate activity under:

1. current globals
2. after proposed `startingPrice`
3. after proposed `minimumPrice`
4. after proposed `stepDecayRate`

If a token becomes active at a stage, add it to that stage's settle list.

### Important behavior

The planner must collect **all** revived empty tokens at each stage, not just one.

Even if only one token is expected in normal operation, the contract model allows multiple `isActive(token) == true` states across enabled tokens.

## CLI / API Changes

### Shared prepare flow

Implement recovery planning in the server-side prepare path so it applies to:

- `tidal kick run`
- `tidal-server kick run`
- API-backed prepare

Likely files:

- [tidal/transaction_service/kicker.py](tidal/transaction_service/kicker.py)
- [tidal/api/services/action_prepare.py](tidal/api/services/action_prepare.py)
- [tidal/chain/contracts/abis.py](tidal/chain/contracts/abis.py)

### Prepare flow shape

Proposed prepare logic:

1. build standard kick
2. estimate standard kick
3. if standard estimate succeeds:
   - return standard prepared action
4. if standard estimate fails for unrelated reason:
   - return normal skip/error
5. if standard estimate fails with `active auction`:
   - run recovery planner
   - build extended single-candidate kick
   - estimate extended kick
   - if extended estimate succeeds, return extended prepared action
   - otherwise return clear blocking details

### Preview output

Expose recovery details in previews.

Text mode examples:

- `Recovery: settle ynRWAx after starting price`
- `Recovery: settle 2 stale empty lots after minimum price`

JSON/API example:

```json
{
  "recoveryPlan": {
    "settleAfterStart": ["0x..."],
    "settleAfterMin": [],
    "settleAfterDecay": []
  }
}
```

### Failure behavior

If recovery planning still cannot produce a sendable sequence:

- return a clear skip reason
- include the blocking token(s)
- include the stage where revival occurs

## Implementation Sequence

1. Add local auction math helper that matches `Auction.sol`.
2. Add recovery planner that returns staged settle lists.
3. Add new extended ABI and types without changing the current path.
4. Add `kickExtended` to `AuctionKicker.sol` and test it.
5. Update Tidal prepare flow to try standard first, extended only on `active auction`.
6. Update preview rendering in CLI and API JSON.
7. Deploy new `AuctionKicker`.
8. Update `auction_kicker_address` in tracked server config.
9. Validate both:
   - standard path still works for normal kicks
   - extended path fixes the fee burner case

## Testing

### Math parity

- off-chain simulation matches on-chain `price()` / `isActive()` for sampled tokens and params

### Planner tests

- token revives after `startingPrice`
- token revives after `minimumPrice`
- token revives after `stepDecayRate`
- multiple revived tokens at the same stage
- no recovery plan when no revival occurs

### Contract tests

- staged settles run in the intended order
- `settleAfterStart` unblocks later setters
- `settleAfterMin` unblocks later setter / kick
- transfer happens after staged settles
- standard `kick` / `batchKick` behavior remains unchanged
- extended entrypoint is only needed for recovery cases

### End-to-end

- reproduce current `opASF` / `ynRWAx` case
- prepare returns staged recovery
- gas estimate succeeds
- kick broadcasts successfully

## Rollout

1. Deploy new `AuctionKicker`.
2. Ensure it has the same governance/mech authorization as the current one.
3. Update `config/server.yaml` with the new address.
4. Restart API and scanner.
5. Verify prepare output shows recovery plan for the fee burner case.
6. Verify `tidal kick run --broadcast --source-type fee-burner` succeeds.

## Recommendation

Implement this as a two-path system:

- standard path for the common case
- extended recovery path only when the standard path fails with `active auction`

Separately, still pursue the upstream `Auction.sol` improvement to make `settle()` more permissive for empty kicked lots. That would remove the need for this workaround over time.
