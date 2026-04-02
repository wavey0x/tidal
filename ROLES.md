# Roles Plan

## Goal

Let the same operator account that is an `AuctionKicker` keeper also run `enable-tokens` on auctions, without requiring that account to be a direct mech on the `TradeHandler`.

## Current split

Today there are two different permission models:

- `kick` / `kickExtended` / `sweepAndSettle`
  - signer only needs to be `owner` or `keeper` on `AuctionKicker`
  - `AuctionKicker` itself is the mech that calls `TradeHandler.execute(...)`

- `enable-tokens`
  - signer must be a direct mech on the auction governance / `TradeHandler`
  - both `tidal-server auction enable-tokens` and API prepare currently target `TradeHandler.execute(...)` directly

This is why one account can kick but cannot enable tokens.

## Recommendation

Add a dedicated `enableTokens` function to `AuctionKicker`, and route all `enable-tokens` execution through that helper.

This feature should support the standard Yearn auction path only.

If an auction does not use the expected `TradeHandler` governance, fail clearly instead of keeping a second execution path.

Do **not** add a generic arbitrary `execute(bytes32[],bytes[])` passthrough to `AuctionKicker`.

That would be too broad. The missing capability is narrow and should stay narrow.

## Why this is the right shape

- reuses the existing keeper role instead of introducing another privileged signer requirement
- keeps the user-facing role model simpler: keeper account can manage kick lifecycle tasks
- is safer than giving keepers a general-purpose `TradeHandler.execute(...)` passthrough
- reuses the same on-chain trust boundary already used for kicking

## Contract plan

### 1. Add a dedicated function on `AuctionKicker`

Add:

```solidity
function enableTokens(address auction, address[] calldata sellTokens) external onlyKeeperOrOwner
```

Behavior:

- require `sellTokens.length > 0`
- require `IAuction(auction).governance() == tradeHandler`
- build wei-roll commands that call `enable(address)` on the auction for each token
- call `ITradeHandler(tradeHandler).execute(commands, state)`

No new storage.
No generic passthrough.

### 2. Keep validation narrow

Do **not** add on-chain discovery heuristics or token probing logic.

The contract should not try to decide whether a token *should* be enabled.
It should only execute the explicit token list prepared off-chain.

## Python / API plan

### 3. Extend the ABI surface

Update `tidal/chain/contracts/abis.py` to include the new `AuctionKicker.enableTokens(...)` ABI entry.

### 4. Centralize `enable-tokens` execution planning around the kicker only

Centralize `enable-tokens` transaction planning in `tidal/ops/auction_enable.py`.

Add one small shared dataclass, e.g. `EnableExecutionPlan`, containing only what the CLI and API need:

- `to_address`
- `data`
- `gas_estimate`
- `call_succeeded`
- `error_message`
- `sender_authorized`
- `authorization_target`

Behavior:

- require `inspection.governance == YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS`
- require `settings.auction_kicker_address` to be configured
- encode `AuctionKicker.enableTokens(auction, tokens)`
- preview by `eth_call` / gas estimate against `AuctionKicker`
- authorization check is only:
  - `AuctionKicker.owner() == sender`
  - or `AuctionKicker.keeper(sender) == true`

If governance does not match or the kicker address is missing, return a clear configuration / unsupported-auction error.

Use this shared plan object from both:

- direct server CLI path
- API prepare path

This avoids duplicating calldata, preview, and keeper-authorization logic.

### 5. API prepare change

Update `prepare_enable_tokens_action` in `tidal/api/services/action_prepare.py`:

- build the token selection exactly as today
- build the shared `EnableExecutionPlan`
- encode `AuctionKicker.enableTokens(auction, tokens)`
- return the kicker target and preview metadata in the prepared action
- fail clearly if the auction does not use the expected governance

## CLI plan

### 6. `tidal-server auction enable-tokens`

Update direct server execution to use the shared execution-plan path.

The command behavior stays the same, but it always sends to `AuctionKicker`.

### 7. `tidal auction enable-tokens`

Update the API-backed CLI to display the same plan cleanly.

The command UX should stay unchanged apart from the new global CLI semantics that already landed:

- mutating commands are live by default
- interactive confirmation is required by default
- `--no-confirmation` is the automation bypass
- `--json` requires `--no-confirmation`

So this feature should plug into the current review-and-send flow rather than reintroducing a preview-only mode.

### 8. Improve wording in preview output

Current wording should stop implying a direct `TradeHandler` mech check.

Use simple kicker-focused wording:

- `Execution target: AuctionKicker 0x...`
- `Keeper authorization: yes/no`

## Scope boundary

### 9. Do not turn `AuctionKicker` into a generic governance router

Avoid:

- arbitrary `execute(...)` passthrough
- arbitrary auction function dispatch
- arbitrary target contracts

That would expand keeper power much more than needed.

The right v1 is a single dedicated helper for token enablement.

## Tests

### 10. Contract tests

Extend `contracts/test/AuctionKicker.t.sol` to cover:

- keeper can call `enableTokens`
- non-keeper cannot
- tokens are enabled through `TradeHandler.execute(...)`
- governance mismatch reverts

### 11. Python unit tests

Add / update tests for:

- shared kicker execution-plan construction in `tests/unit/test_auction_enable.py`
- API prepare using `AuctionKicker` in `tests/unit/test_action_prepare.py`
- direct CLI happy path in `tests/unit/test_operator_auction_cli.py`
- direct CLI `--json` + `--no-confirmation` path in `tests/unit/test_operator_auction_cli.py`
- governance mismatch rejection in the relevant CLI/API tests

### 12. Integration tests

Update `tests/integration/test_api_control_plane.py` to assert the prepared transaction target for `enable-tokens` is always:

- `settings.auction_kicker_address`

## Deployment plan

### 13. On-chain rollout

1. deploy the updated `AuctionKicker`
2. add the new `AuctionKicker` as a mech on `TradeHandler`
3. grant keeper access to the intended operator account(s)
4. update `auction_kicker_address` in server config

### 14. App rollout

1. deploy updated Tidal API/server
2. upgrade CLI installs as needed
3. verify `enable-tokens` with:
   - keeper-only account on a standard governance auction
   - governance mismatch rejection on a non-standard auction

## Downstream note

Checked the read-only `wavey-api` repo for a Tidal proxy/service layer tied to this payload shape and did not find an active `services/tidal.py` or route integration that would need changes for this feature.

So this plan is contained to:

- Solidity contract
- Tidal API prepare path
- Tidal CLIs
- tests/docs
