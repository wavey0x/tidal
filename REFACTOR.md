# Auction Lot Resolver Refactor

## Thesis

The right design is to move auction closeout logic into `contracts/src/AuctionKicker.sol` and make the CLI a thin shell around a single contract entrypoint.

Today, too much lifecycle knowledge lives off-chain:

- the CLI has to reason about whether to settle, sweep, force sweep, or do nothing
- the API has to inspect auction edge cases and predict whether a lot is recoverable
- recovery behavior is partially modeled off-chain in `tidal/transaction_service/auction_recovery.py`
- `AuctionKicker` exposes recovery complexity through narrow functions instead of owning the full lot-resolution state machine

From first principles, this is backwards. The mech has the permissions and atomic execution environment. The keeper CLI should not need to understand auction edge cases deeply. It should only need to say: resolve this lot.

## Core Contract Facts

The verified auction contract behaves like this:

- `isActive(_from)` is price-driven, not balance-driven.
- `available(_from)` returns `0` whenever `isActive(_from) == false`.
- `settle(_from)` requires both:
  - `isActive(_from) == true`
  - `ERC20(_from).balanceOf(address(this)) == 0`
- `price(_from)` returns `0` when any of the following are true:
  - the lot has zero effective available amount
  - the lot is expired by time
  - the computed price is below `minimumPrice`

This creates the awkward lifecycle states we have been fighting:

- a lot can be inactive even though the auction still holds sell tokens
- a lot can be active even though the auction balance is already zero

That is exactly why the contract, not the CLI, should own the resolution logic.

## Design Goal

We want one keeper-controlled function in `AuctionKicker` that can clear tokens out of both live and non-live auctions and leave the lot in a clean terminal state.

That function should:

1. inspect the lot on-chain
2. classify its state
3. choose one deterministic resolution path
4. execute that path atomically through `TradeHandler.execute(...)`

The CLI should prepare and send a single action, not encode lifecycle policy.

## Resolver Invariants

The single function should be designed around a few hard guarantees:

- if the auction holds sell tokens, the function must either clear them out or revert
- if the lot is settleable, the function should end by settling it
- if the lot is not settleable because it is expired or never kicked, the function should end by disabling it
- the function should never require the CLI to choose between sweep, settle, rescue, or disable
- recovered sell tokens should always return to the auction receiver

Those invariants are what let the CLI collapse to one operator action without losing safety.

## Proposed Contract API

Add one new external function to `AuctionKicker`:

```solidity
function resolveLot(address auction, address sellToken) external onlyKeeperOrOwner;
```

This function supersedes the current narrow recovery behavior of `sweepAndSettle(...)`.

If backward compatibility matters, `sweepAndSettle(...)` can remain temporarily as a thin wrapper that delegates to `resolveLot(...)` for the active-lot cases, but the long-term design should expose only one operator-facing closeout primitive.

## Required Auction Interface Additions

To make `resolveLot(...)` self-sufficient, `contracts/src/interfaces/IAuction.sol` should expose everything needed to classify and rescue a lot:

- `function auctionLength() external view returns (uint256);`
- `function stepDuration() external view returns (uint256);`
- `function setStepDuration(uint256) external;`
- `function kicked(address _from) external view returns (uint256);`
- `function disable(address _from) external;`
- `function auctions(address _from) external view returns (uint64 kicked, uint64 scaler, uint128 initialAvailable);`

The public mapping getter for `auctions(address)` matters because the resolver should be able to derive rescue parameters from the actual lot metadata instead of relying on CLI guesses.

## Single-Function State Machine

`resolveLot(...)` should classify the lot using these inputs:

- `isActive(sellToken)`
- `ERC20(sellToken).balanceOf(auction)`
- `kicked(sellToken)`
- `auctionLength()`
- current timestamp
- lot metadata from `auctions(sellToken)`

The resolver only needs to know:

- is the lot active?
- is the lot expired by time?
- is there still sell-token balance in the auction?
- has the lot ever been kicked?

### State Table

| State | Conditions | Resolution Path |
|---|---|---|
| 1. Active, balance > 0 | `isActive == true`, auction token balance `> 0` | Sweep from auction, transfer swept tokens to receiver, settle |
| 2. Active, balance == 0 | `isActive == true`, auction token balance `== 0` | Settle only |
| 3. Inactive, unexpired, balance > 0 | `isActive == false`, `kicked != 0`, `now <= kicked + auctionLength`, balance `> 0` | Apply rescue profile, sweep to receiver, settle, restore original params |
| 4. Inactive, unexpired, balance == 0 | `isActive == false`, `kicked != 0`, `now <= kicked + auctionLength`, balance `== 0` | Apply rescue profile, settle, restore original params |
| 5. Inactive, expired, balance > 0 | `isActive == false`, `kicked != 0`, `now > kicked + auctionLength`, balance `> 0` | Sweep to receiver, disable |
| 6. Inactive, expired, balance == 0 | `isActive == false`, `kicked != 0`, `now > kicked + auctionLength`, balance `== 0` | Disable |
| 7. Never kicked, balance > 0 | `kicked == 0`, balance `> 0` | Sweep to receiver, disable |
| 8. Never kicked, balance == 0 | `kicked == 0`, balance `== 0` | Disable or no-op, depending on whether the token is still enabled |

This is the complete lifecycle surface the contract should own.

The important point is that every state with non-zero auction balance has a token-clearing path, regardless of whether the lot is currently live, inactive, expired, or never kicked.

## Rescue Profile For Unexpired Inactive Lots

The hardest case is the below-floor but not-yet-expired lot.

For those states, the resolver should temporarily move the auction into a deterministic rescue configuration that makes `settle(...)` possible again, then restore the original globals afterward.

The rescue profile should be computed and executed on-chain.

### Rescue Steps

1. Snapshot the original globals:
   - `startingPrice`
   - `minimumPrice`
   - `stepDecayRate`
   - `stepDuration`
2. Set rescue globals:
   - `minimumPrice = 0`
   - `stepDecayRate = 1`
   - `stepDuration = auctionLength - 1`
   - `startingPrice = rescueStartingPrice`
3. If the auction still holds balance, sweep and transfer it back to the receiver
4. Call `settle(sellToken)`
5. Restore the original globals

### Why This Rescue Profile

`minimumPrice = 0` alone is good but not fully general. It revives the common below-floor case, but it does not guarantee `isActive()` becomes true in every unexpired state if the computed price itself has decayed to zero under extreme parameters.

So the rescue profile should not rely on only one knob.

The deterministic rescue profile above is better because:

- lowering `minimumPrice` removes the floor gate
- lowering `stepDecayRate` flattens the decay curve
- increasing `stepDuration` minimizes the number of elapsed decay steps
- increasing `startingPrice` guarantees the recomputed price is positive

### Rescue Starting Price

`startingPrice` should not be guessed off-chain.

The contract should compute a `rescueStartingPrice` from lot metadata so that the recomputed price is guaranteed to be positive for the current lot while still unexpired. The necessary inputs are available from the lot's stored `initialAvailable` and `scaler`.

The important design point is not the exact formula here, but that the formula belongs in Solidity, alongside the lot state machine, not in the CLI.

## Safety Assumption

This design relies on an important system assumption:

- only one lot can ever be active at once

That assumption materially changes the tradeoff. Because only one lot can be active at a time, temporarily mutating auction-global parameters during `resolveLot(...)` is acceptable. We are not risking accidental reactivation of multiple live lots during the same rescue flow.

If that invariant is not actually guaranteed in production, this design becomes unsafe and should be reconsidered.

## Receiver Handling

Recovered sell tokens should always be sent back to `IAuction(auction).receiver()`.

The resolver should not accept an arbitrary recipient argument. The keeper's job is to resolve lot state, not redirect assets.

## Events

The single function should emit one event that records the chosen path.

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

Where `path` identifies which state branch executed.

This is better than proliferating one event per narrow helper because it mirrors the single-entrypoint design.

## Impact On `AuctionKicker`

This refactor should simplify the contract's surface area even if the internal logic gets more thoughtful.

### External Surface

The desired long-term public surface is:

- `kick(...)`
- `batchKick(...)`
- `kickExtended(...)` only if still needed for true kick-time behavior
- `enableTokens(...)`
- `resolveLot(...)`

### What Should Shrink Or Disappear

- the current recovery-specific meaning of `sweepAndSettle(...)`
- CLI-side lifecycle policy for auction closeout
- off-chain staged recovery planning for empty lots
- special operator flags whose only purpose is to compensate for settlement edge cases

In particular, `tidal/transaction_service/auction_recovery.py` is a sign that the recovery brain is in the wrong place. That logic should move on-chain or disappear.

## CLI And API Direction

The operator experience should collapse to one command:

```bash
tidal auction resolve 0xAuction --token 0xSellToken
```

The API prepare route should do only enough inspection to present a preview of the predicted branch.

The CLI should not need to decide among:

- normal settle
- forced sweep
- below-floor rescue
- sweep and disable

Those are contract concerns.

### CLI Responsibilities After Refactor

The CLI should only:

1. collect auction and token input
2. call the prepare endpoint
3. show the predicted branch in the preview
4. sign and send the prepared transaction

That is the correct boundary.

## Recommendation

Implement one on-chain lot resolver in `AuctionKicker`.

Do not keep growing CLI-side heuristics to work around auction state inconsistencies. Those inconsistencies are exactly why the closeout policy belongs in the mech.

The contract should own the full state machine for:

- live non-empty lots
- live empty lots
- inactive but unexpired lots
- expired lots
- never-kicked cleanup states

Once that exists, the CLI becomes simpler, the keeper workflow becomes more reliable, and the system stops depending on off-chain reasoning to recover from auction edge cases.
