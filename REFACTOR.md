# Auction Recovery Refactor

## Problem

We need a way for the keeper to recover tokens from auctions that are no longer settleable but still hold inventory. This happens when a lot falls below `minimumPrice`: the auction contract marks the lot inactive, `available(token)` returns `0`, and `settle(token)` is no longer valid because settlement only works for active, empty lots. As a result, tokens can remain stranded inside the auction even though the auction is no longer live.

Today, the keeper EOA can execute actions through `AuctionKicker`, but it is not itself a mech on the `TradeHandler`. That means the keeper cannot directly use `TradeHandler.execute(...)` or call the auction's governance-only recovery functions. The existing `sweepAndSettle` path in `AuctionKicker` also does not solve this case, because it depends on the lot still being active.

## Solution

The fix is to extend `contracts/src/AuctionKicker.sol` with a dedicated recovery operation for inactive lots, such as `sweepAndDisable(auction, sellToken)`. This function would remain keeper-controlled through `AuctionKicker`, but would use the mech's `TradeHandler.execute(...)` permissions to perform the recovery on-chain.

The recovery flow is:

1. Sweep the full stranded token balance out of the auction.
2. Transfer the recovered balance back to the auction receiver.
3. Disable that sell token in the auction so the lot is fully cleaned up.

This keeps all auction management flowing through `AuctionKicker`, matches the current permission model, and gives the keeper a safe way to unwind unsold inactive auctions without requiring direct mech or governance privileges on the keeper EOA. In the CLI, this should be exposed as a separate recovery action rather than overloading normal settlement, since it is solving a different lifecycle state: inactive stranded inventory, not active-lot settlement.
