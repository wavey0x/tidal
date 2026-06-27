# LP Direct Deposit Plan

## Recommendation

Do not put the `if rewardToken is in lp.coins()` decision inside Solidity.

That decision belongs in Tidal's prepare workflow, before the current
auction-only path groups candidates by auction, checks stuck auction state, and
asks the quote API for auction pricing. The prepare workflow has the full
candidate context: source type, source address, reward token, live balance,
strategy or fee-burner want, USD threshold, gas estimation, and existing operator
policy.

The contract should be a stable TradeHandler execution surface. It should not
discover LPs, iterate arbitrary pool shapes, choose routes, or decide whether to
auction. It also should not need a new top-level method every time we find a new
non-auction opportunity.

## Current Workflow Facts

- `contracts/src/AuctionKicker.sol` is a minimal TradeHandler mech. It builds
  fixed Weiroll command programs and calls the hardcoded Yearn TradeHandler at
  `0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b`.
- The current `kick(...)` path validates auction-specific invariants:
  `auction.governance() == tradeHandler`, `auction.want() == wantToken`,
  `sellToken != wantToken`, and `auction.receiver() == source`.
- `tidal/transaction_service/planner.py` is currently auction-centered. It
  groups shortlisted candidates by auction, resolves or skips blocked auctions,
  then prepares `PreparedKick` objects.
- `tidal/transaction_service/kick_prepare.py` always prepares quote-based auction
  kicks. It skips same-token and same-symbol cases, checks live balance, then
  calls `/v1/quote` to compute auction start and minimum prices.
- `tidal/transaction_service/kick_tx.py` only encodes
  `AuctionKicker.kick(...)`, `AuctionKicker.batchKick(...)`, and
  `AuctionKicker.resolveAuction(...)`.
- The DB already has `kick_txs.operation_type`, so the data model can represent
  a new operation such as `lp_deposit` without pretending it is an auction kick.

## Where The Logic Should Live

### Tidal prepare workflow: yes

Add route selection in the planner/preparer before auction settlement inspection.

For each shortlisted candidate:

1. Read the candidate's intended want:
   - strategy source: `strategy.want()`
   - fee-burner source: configured `want_address`
2. Treat that want as a possible LP.
3. Probe supported LP interfaces off-chain.
4. If the reward token appears in `lp.coins(i)` and the LP deposit adapter is
   supported, prepare an `lp_deposit` operation.
5. Otherwise, continue through the current auction kick path.

This placement is important. An LP deposit does not need the auction to be idle,
does not need auction settlement, and does not need a quote. If this routing
happens after the existing auction checks, a perfectly valid LP deposit could be
incorrectly blocked by a live or dirty auction.

### Contract route selection: no

The contract should not contain broad branching such as:

```solidity
if (rewardToken is in lp.coins()) {
    deposit();
} else {
    auction();
}
```

That would mix policy, discovery, and execution in the most expensive and least
observable layer. It also creates unclear failure modes because Curve-style LPs
do not share one universal `deposit()` ABI.

### Contract execution authority: still needed

Tidal cannot simply send LP deposit calls directly from the keeper EOA, because
the funds sit in strategy or fee-burner contracts and the existing permission
model is built around the Yearn TradeHandler. The current auction path works
because a TradeHandler mech builds commands that make the TradeHandler transfer
tokens from the source.

So the clean split is:

- Tidal decides `auction_kick` vs `lp_deposit`.
- A stable TradeHandler operator executes the selected operation through an
  approved route adapter.

## Extensibility Requirement

This should not become "add one Solidity method and one config migration for
each new protocol."

Build the first LP route as part of a generic route framework:

- Tidal has off-chain route adapters that detect, preview, and prepare
  opportunities.
- The on-chain operator has a stable `executeRoute` API.
- Protocol-specific on-chain route adapters build tightly constrained
  TradeHandler command programs.
- New routes can be added by adding a new off-chain adapter and, only when
  needed, registering a new on-chain route adapter.
- The main operator address should not change when adding a new route.
- The service config should keep one operator address, not one address per route.
- Logging should store generic route metadata so future route fields do not force
  a DB migration every time.

There are two levels of future updates:

1. New opportunity supported by an existing route adapter: add target metadata or
   off-chain detection logic only.
2. New protocol requiring new execution rules: deploy one small route adapter and
   register it on the existing operator. Do not redeploy the main operator and do
   not change Tidal's operator address.

## Contract Shape

Because this new action is not an auction, the cleanest breaking-change design is
to replace the `AuctionKicker` concept with a more accurately named stable
operator, for example `TradeHandlerOperator`.

That avoids putting LP deposit methods on a contract named `AuctionKicker`, keeps
one operator contract address in Tidal, and avoids a second long-lived config
address.

Planned breaking changes:

- Rename the deployed contract concept from `AuctionKicker` to
  `TradeHandlerOperator`.
- Replace `auction_kicker_address` with `trade_handler_operator_address`.
- Keep the existing auction functions as typed auction operations on the new
  operator.
- Add a route-adapter execution API for LP deposits and future non-auction
  opportunities.
- Do not add compatibility aliases for the old contract/config names.

The contract should still reject arbitrary Weiroll payloads. It should expose
only typed auction methods and approved route adapters.

## Stable Route API Sketch

Initial stable operator surface:

```solidity
interface ITradeRouteAdapter {
    function build(bytes calldata params)
        external
        view
        returns (bytes32[] memory commands, bytes[] memory state);
}

struct RouteCall {
    bytes32 routeId;
    bytes params;
}

function setRouteAdapter(bytes32 routeId, address adapter, bool enabled) external onlyOwner;
function executeRoute(bytes32 routeId, bytes calldata params) external onlyKeeperOrOwner;
function batchExecuteRoutes(RouteCall[] calldata calls) external onlyKeeperOrOwner;
```

The operator flow:

1. Look up `routeId` in the operator's adapter registry.
2. Revert if the route is not enabled.
3. Call the adapter's `build(params)` method.
4. Pass the returned commands/state to the hardcoded TradeHandler.
5. Emit a generic `RouteExecuted(routeId, adapter, paramsHash)` event.

This is flexible without accepting arbitrary keeper-provided command arrays.
Keepers provide typed params. The registered adapter is responsible for
validating those params and building a narrow command program.

Initial LP route params can be encoded as:

```solidity
struct CurveLpDepositParams {
    address source;
    address lp;
    address rewardToken;
    uint256 rewardAmount;
    uint8 coinIndex;
    uint8 coinCount;
    uint8 poolKind;
    uint256 minLpOut;
    address lpToken;
}
```

Initial LP adapter validation:

- `source`, `lp`, `rewardToken`, and `lpToken` are nonzero.
- `rewardAmount` is nonzero.
- `minLpOut` is supplied by Tidal.
- For strategy sources, `IStrategy(source).want() == lpToken` or `lp`, based on
  the audited pool shape.
- `ICurvePoolCoins(lp).coins(coinIndex) == rewardToken`.
- `coinCount` and `poolKind` are one of the small set supported by the adapter.

Execution, through TradeHandler:

1. `rewardToken.transferFrom(source, tradeHandler, rewardAmount)`
2. `rewardToken.approve(lp, rewardAmount)`
3. call the supported LP deposit method with only one nonzero coin amount
4. make sure the minted LP tokens end at `source`
5. emit the generic route event from the operator

The "LP tokens end at source" invariant is non-negotiable. Some Curve pools mint
to `msg.sender`, while others support a receiver argument. If a pool mints to the
TradeHandler, the command sequence must transfer the LP token back to `source`.
If that cannot be done safely for a pool kind, that pool kind should not be
enabled in the adapter.

## Pool Adapter Scope

Do not assume `deposit()` is universal.

For Curve-style pools, `lp.coins(i)` is a useful identification check, but the
deposit method is usually some form of `add_liquidity(...)`, and the exact ABI
varies by pool family:

- 2-coin pools may use `add_liquidity(uint256[2], uint256)` or a receiver
  overload.
- 3-coin pools use a different fixed-size array.
- Some pools use zaps or wrapper contracts.
- yCRV-related paths may have their own deposit surface.

Start with a small set of explicit adapters:

1. 2-coin Curve pool with verified receiver behavior.
2. 3-coin Curve pool only if needed for the first target set.
3. yCRV or zap adapter only after confirming the exact deposit ABI and receiver
   behavior.

Each adapter needs:

- `coins(i)` detection
- `calc_token_amount(amounts, true)` or equivalent preview
- deposit calldata construction
- receiver/minted-token handling rules

If preview or receiver behavior is not known, Tidal should not prepare an
`lp_deposit` operation for that candidate.

## Tidal Changes

Add a new prepared route operation type, not fields on `PreparedKick`:

```python
PreparedRouteOperation(
    candidate,
    operation_type="lp_deposit",
    route_id,
    route_params,
    route_metadata,
    tx_operation="route",
)
```

Then model LP-specific metadata in `route_metadata`, not as permanent core
fields:

```python
{
    "lpAddress": "...",
    "lpToken": "...",
    "rewardToken": "...",
    "rewardAmount": "...",
    "normalizedRewardAmount": "...",
    "coinIndex": 0,
    "coinCount": 2,
    "poolKind": "curve-2coin",
    "minLpOut": "...",
    "expectedLpOut": "...",
    "slippageBps": 50,
}
```

Planner changes:

- Split shortlisted candidates into LP-deposit candidates and auction candidates
  before auction settlement inspection.
- LP-deposit candidates skip auction settlement inspection and quote pricing.
- Auction candidates continue through the current path unchanged.
- Add `route_operations` or `lp_deposit_operations` to `KickPlan`. Prefer a
  generic `route_operations` list if it keeps execution readable.
- Include `lp-deposit` items in `preparedOperations`.
- Build `executeRoute` or `batchExecuteRoutes` tx intents.
- Execute and dry-run LP deposits as first-class operations.
- Persist `operation_type = "lp_deposit"` in `kick_txs`.
- Add one generic JSON metadata column for prepared/executed route details, such
  as `operation_metadata_json`, so future routes do not require schema changes
  only to display route-specific fields.

ABI/config changes:

- Add the new operator ABI to `tidal/chain/contracts/abis.py`. The stable route
  ABI should not need to change when adding a new route.
- Replace `auction_kicker_address` with `trade_handler_operator_address`.
- Update runtime wiring, server templates, docs, and CLI labels.

## Tidal Route Adapter Shape

Add an off-chain adapter interface used by the planner:

```python
class RouteAdapter(Protocol):
    operation_type: str
    route_id: bytes

    async def maybe_prepare(candidate: KickCandidate) -> PreparedRouteOperation | None:
        ...
```

The first implementation can be `CurveLpDepositRouteAdapter`.

The planner should receive an ordered list of route adapters. For each candidate,
it asks adapters whether they can prepare a non-auction operation. The first
successful adapter wins. If no adapter prepares an operation, the candidate falls
through to the existing auction path.

This keeps future additions localized:

- add a new adapter class
- add tests for detection/preview/params
- register the adapter in runtime wiring
- only deploy/register an on-chain adapter if the route needs new execution
  rules

## Slippage And Min-Out

Tidal should compute `minLpOut` off-chain before encoding the transaction.

Recommended initial policy:

- call the adapter's LP preview method, such as `calc_token_amount`
- apply a small code default slippage buffer
- do not reuse auction price buffers for LP deposit slippage
- add a new config only if production pools prove the default is not adequate

The default should be conservative enough to avoid dust/min-out failures but not
so wide that it masks broken pool metadata.

## Target Route Audit

Before implementation, collect exact data for the first target set:

- BOLD-USDC LP
- DOLA LPs
- yCRV strategies

For each target:

- source strategy or fee-burner
- want address
- LP/pool address
- LP token address if different from pool address
- `coins(i)` outputs
- reward tokens expected to match coins
- deposit function signature
- preview function signature
- whether deposit supports a receiver argument
- where LP tokens are minted
- whether one-sided deposit is supported
- whether the existing route adapter can support it
- if not, what new adapter or params are required

This audit determines which adapter(s) to implement first.

## Tests

Solidity tests:

- keeper can execute an LP deposit when `coins(coinIndex) == rewardToken`
- revert when `coins(coinIndex) != rewardToken`
- revert when strategy `want()` does not equal the LP
- revert unauthorized callers
- minted LP tokens end at `source`
- batch LP deposit works
- existing auction kick behavior still works on the renamed operator
- route adapter registry can enable and disable the LP route
- keeper cannot execute an unregistered route

Python tests:

- LP-eligible candidate prepares `lp_deposit`
- LP-eligible candidate does not call quote pricing
- LP-eligible candidate is not blocked by auction settlement inspection
- unsupported LP falls back to the normal auction path
- unsupported or failed LP preview records a clear prepare skip/fallback reason
- tx builder encodes `executeRoute` or `batchExecuteRoutes`
- executor persists `operation_type = "lp_deposit"`
- preview payload shows `operation = "lp-deposit"`
- route metadata is included in preview and persisted without route-specific DB
  columns

## Difficulty

This is medium if the first target pools share one or two Curve-style deposit
ABIs.

It becomes high if we try to support arbitrary LPs generically inside one
adapter. The right scope is a stable route framework plus small adapters with
explicit preview and receiver handling.

The highest-risk parts are not the `coins(i)` check. They are:

- proving the deposit ABI for each target pool family
- guaranteeing minted LP tokens end at the source
- computing a reliable `minLpOut`
- placing route selection before auction settlement/quote logic

## Implementation Order

1. Audit the first target pools and write down their adapter requirements.
2. Refactor contract naming from `AuctionKicker` to `TradeHandlerOperator`.
3. Add the stable route adapter registry and `executeRoute` /
   `batchExecuteRoutes` API.
4. Add the first on-chain route adapter for the first supported Curve LP pool
   kind.
5. Add Foundry tests with mocks for route registry and LP deposit invariants.
6. Add Tidal route adapter infrastructure and the first Curve LP adapter.
7. Add generic route operation preview, tx building, execution, dry-run, and
   metadata persistence.
8. Replace `auction_kicker_address` config with `trade_handler_operator_address`.
9. Run dry-run previews against the target candidates.
10. Deploy the new operator, allowlist it as a TradeHandler mech, register the
    first route adapter, update server config, and run live with a narrow target
    filter first.
