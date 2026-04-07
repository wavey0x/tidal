# Auction Recovery Refactor

## Problem

We need a way for the keeper to recover tokens from auctions that are no longer trivially settleable but still need lifecycle cleanup. The current auction contract uses `isActive(_from) = price(_from, now) > 0`, which means activity is price-driven rather than balance-driven.

That creates two awkward states:

1. A lot can still be within the auction timestamp window, but if price falls below `minimumPrice` it becomes inactive. In that state, `available(token)` returns `0` even if the auction still holds the sell token balance, so `sweepAndSettle` cannot proceed.
2. A lot can also report `isActive() == true` even when the auction's current sell token balance is `0`, because `isActive()` is derived from pricing and kick metadata rather than live balance. This makes the settlement lifecycle feel inconsistent and bug-prone.

The keeper EOA can execute actions through `AuctionKicker`, but it is not itself a mech on the `TradeHandler`. That means the keeper cannot directly use `TradeHandler.execute(...)` or call the auction's governance-only recovery functions. We need a recovery path that still flows through `contracts/src/AuctionKicker.sol`.

## Proposed Solution

The best path for the below-floor but not yet expired case is to temporarily override `minimumPrice` in order to make the lot settleable again, then restore it after cleanup.

This would be implemented in `contracts/src/AuctionKicker.sol` as a new keeper-controlled recovery operation that uses the mech's `TradeHandler.execute(...)` permissions to do the following atomically:

1. Read and store the current auction `minimumPrice`.
2. Temporarily set `minimumPrice` to a fake low value such as `0`.
3. If the auction still holds the sell token balance, sweep it out and transfer it back to the auction receiver.
4. Call `settle(sellToken)`.
5. Restore the original `minimumPrice`.

This works because `settle()` only requires:

- `isActive(_from) == true`
- `ERC20(_from).balanceOf(address(this)) == 0`

Lowering `minimumPrice` can make `price(_from, now) > 0` again as long as the lot is still within `auctionLength`. After sweeping the balance to `0`, the auction remains settleable because `isActive()` is not balance-aware.

## Constraints

This minimum-price override approach is only valid under the following conditions:

1. The lot must still be within the auction timestamp window. If the auction has already expired by time, lowering `minimumPrice` will not make it active again.
2. `setMinimumPrice()` is auction-wide, not token-specific.
3. `setMinimumPrice()` can only be called when `!isAnActiveAuction()`, so this path only works when the auction contract has no other active lot at that moment.
4. The original `minimumPrice` should be restored in the same atomic transaction.

## Fallback

For auctions that cannot be reactivated with a temporary `minimumPrice` override, we still need a fallback unwind path. That fallback is a dedicated `sweepAndDisable(auction, sellToken)` style operation:

1. Sweep the full stranded token balance out of the auction.
2. Transfer the recovered balance back to the auction receiver.
3. Disable that sell token in the auction.

This fallback is for lots that are truly expired, or for cases where changing `minimumPrice` is not possible or not safe.

## CLI Direction

This should be exposed as a dedicated recovery action in the CLI rather than being hidden behind the normal `settle` command. The operator should be able to tell whether the recovery path is:

1. `minimumPrice` override plus settle, or
2. sweep plus disable fallback.
