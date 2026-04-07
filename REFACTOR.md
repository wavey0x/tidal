# Auction Resolver Refactor

## Goal

Move auction closeout policy into `contracts/src/AuctionKicker.sol` and make the CLI a thin wrapper around one keeper-facing function:

```solidity
function resolveAuction(address auction, address sellToken) external onlyKeeperOrOwner;
```

Despite the name, this resolves one `sellToken` lot within an auction.

## Problem

The current closeout path is built around `available(sellToken)`. That is the wrong primitive.

In the verified auction contract:

- `isActive(_from)` is price-driven, not balance-driven
- `available(_from)` returns `0` whenever `isActive(_from) == false`
- `settle(_from)` requires:
  - `isActive(_from) == true`
  - `ERC20(_from).balanceOf(address(this)) == 0`
- `sweep(_token)` sends the full balance to `msg.sender`, not to `receiver`
- `disable(_from)` and `enable(_from)` are per-token governance actions with no `isAnActiveAuction()` guard

That means a lot can be inactive while the auction still holds tokens, and `available()` will hide that inventory. The resolver should be balance-driven instead.

## Contract Shape

`resolveAuction(...)` should:

- read `auctionBalance = IERC20(sellToken).balanceOf(auction)`
- read `active = IAuction(auction).isActive(sellToken)`
- read `kickedAt = IAuction(auction).kicked(sellToken)`
- read `receiver = IAuction(auction).receiver()`
- require `IAuction(auction).governance() == tradeHandler`
- choose one deterministic path

Required `IAuction` additions:

- `function kicked(address _from) external view returns (uint256);`
- `function disable(address _from) external;`

Required `AuctionKicker` addition:

```solidity
bytes4 internal constant DISABLE_SELECTOR = bytes4(keccak256("disable(address)"));
```

No global price setter support is needed for this design.

## Resolver Paths

### Path 1: Sweep And Settle

Conditions:

- `active == true`
- `auctionBalance > 0`

Batch:

1. `sweep(sellToken)` from auction to `TradeHandler`
2. `sellToken.transfer(receiver, auctionBalance)` from `TradeHandler`
3. `settle(sellToken)`

This works because settlement is price-driven, not balance-driven. Sweeping to zero balance does not make an active lot inactive inside the same transaction.

### Path 2: Settle Only

Conditions:

- `active == true`
- `auctionBalance == 0`

Batch:

1. `settle(sellToken)`

This handles the "active but empty" case directly.

### Path 3: Sweep And Reset

Conditions:

- `active == false`
- `kickedAt != 0`
- `auctionBalance > 0`

Batch:

1. `sweep(sellToken)` from auction to `TradeHandler`
2. `sellToken.transfer(receiver, auctionBalance)` from `TradeHandler`
3. `disable(sellToken)`
4. `enable(sellToken)`

This is the correct recovery path for inactive kicked lots that still hold inventory. It clears tokens first, then resets the stale lot state locally.

### Path 4: Reset Only

Conditions:

- `active == false`
- `kickedAt != 0`
- `auctionBalance == 0`

Batch:

1. `disable(sellToken)`
2. `enable(sellToken)`

This solves the hard state: inactive, empty, and stuck with `kicked != 0`. No global mutation, no pricing assumptions, no relist step required.

### Path 5: Sweep Only

Conditions:

- `active == false`
- `kickedAt == 0`
- `auctionBalance > 0`

Batch:

1. `sweep(sellToken)` from auction to `TradeHandler`
2. `sellToken.transfer(receiver, auctionBalance)` from `TradeHandler`

There is no stale kick state to clean up. Once the balance is removed, the lot is already clean enough for future use.

### Path 6: No-Op

Conditions:

- `active == false`
- `kickedAt == 0`
- `auctionBalance == 0`

Batch:

1. none

## State Table

| State | Conditions | Resolution |
|---|---|---|
| 1. Active, balance > 0 | `isActive == true`, balance `> 0` | Sweep and settle |
| 2. Active, balance == 0 | `isActive == true`, balance `== 0` | Settle only |
| 3. Inactive, unexpired, balance > 0 | `isActive == false`, `kicked != 0`, balance `> 0` | Sweep and reset |
| 4. Inactive, unexpired, balance == 0 | `isActive == false`, `kicked != 0`, balance `== 0` | Reset only |
| 5. Inactive, expired, balance > 0 | `isActive == false`, `kicked != 0`, balance `> 0` | Sweep and reset |
| 6. Inactive, expired, balance == 0 | `isActive == false`, `kicked != 0`, balance `== 0` | Reset only |
| 7. Clean, balance > 0 | `kicked == 0`, balance `> 0` | Sweep only |
| 8. Clean, balance == 0 | `kicked == 0`, balance `== 0` | No-op |

The resolver does not need to care why an inactive kicked lot is inactive. Below-floor and expired lots use the same local reset path.

## Why State 4 Uses `disable -> enable`

State 4 is the genuinely hard case:

- inactive
- empty
- `kicked != 0`

It cannot be settled, and there are no tokens left to re-kick.

`disable -> enable` is the right solution because it:

- is local to the token
- has no `isAnActiveAuction()` guard
- resets `kicked` to `0`
- restores scaler and relayer approval
- leaves the token ready for future kicks

This is cleaner than a no-op if the goal is to leave the lot reusable immediately.

## Non-Goals

This resolver should not:

- mutate `startingPrice`, `minimumPrice`, `stepDecayRate`, or `stepDuration`
- reproduce the `kickExtended(...)` recovery planner on-chain
- depend on `forceKick(...)` for inactive-lot closeout

Reasons:

- setter-based rescue is blocked by `isAnActiveAuction()` and reintroduces sequencing complexity
- `forceKick(...)` still depends on pricing globals and does not solve the empty stale-lot case
- `disable -> enable` is the simpler general reset primitive

## Event

Use a single event:

```solidity
event AuctionResolved(
    address indexed auction,
    address indexed sellToken,
    uint8 path,
    address receiver,
    uint256 recoveredBalance
);
```

Suggested path values:

- `0`: no-op
- `1`: settle only
- `2`: sweep only
- `3`: sweep and settle
- `4`: reset only
- `5`: sweep and reset

Replacing `SweepAndSettled(auction, sellToken)` with `AuctionResolved(...)` is an indexer-facing breaking change. Dual-emitting on the sweep-and-settle path is optional if compatibility matters.

## CLI Direction

The CLI should only prepare and preview one of these outcomes:

- sweep and settle
- settle only
- sweep and reset
- reset only
- sweep only
- no-op

The CLI should not:

- inspect `available()` to decide recoverability
- model pricing rescue behavior
- generate `settleAfterStart` / `settleAfterMin` / `settleAfterDecay` closeout plans

## Recommendation

Implement `resolveAuction(...)` as a minimal balance-driven resolver.

That directly solves the stuck-auction problem, handles State 4 cleanly, avoids global mutation, and removes closeout complexity from the CLI.
