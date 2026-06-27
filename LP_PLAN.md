# LP Direct Deposit Plan

## Decision

Build this as a route-aware operator workflow, not as an `AuctionKicker` special
case.

The routing decision belongs in Tidal. The contract should execute a route that
Tidal already selected and validated. It should not discover LPs, inspect
`coins()` loops to choose behavior, call quote APIs, or decide whether to auction.

The clean shape is:

1. Tidal scans and shortlists reward-token candidates as it does today.
2. Tidal asks non-auction route adapters whether a better direct action exists.
3. If a safe route exists, Tidal prepares that route and removes the candidate
   from the auction path.
4. If no route applies, the existing auction kick path handles the candidate.
5. A stable TradeHandler operator contract executes either typed auction actions
   or approved route-module actions.

This gives us LP deposits now and a clean place for future non-Curve opportunities
without adding one-off config, duplicate contracts, or route-specific database
columns each time.

## Non-Goals

- Do not put `if rewardToken is in lp.coins()` branching inside the auction kick
  function.
- Do not expose arbitrary keeper-supplied Weiroll commands.
- Do not try to support every Curve pool or every LP deposit ABI in the first
  implementation.
- Do not use prices or quote APIs for LP deposits.
- Do not keep backwards-compatible config names, contract names, APIs, or legacy
  code paths. Breaking changes are fine.

## Current Constraints

- `contracts/src/AuctionKicker.sol` is auction-specific. It validates auction
  governance, want, receiver, and active state, then builds fixed TradeHandler
  Weiroll commands.
- The hardcoded Yearn TradeHandler is
  `0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b`.
- `tidal/transaction_service/planner.py` currently groups candidates by auction
  before prepare. That is wrong for LP deposits because an LP deposit does not
  care whether the related auction is idle, live, or dirty.
- `tidal/transaction_service/kick_prepare.py` currently prepares only quote-based
  auction kicks.
- `tidal/transaction_service/kick_tx.py` only encodes auction-kicker calls.
- `kick_txs` is mostly auction-shaped. It has `operation_type`, but the table and
  many columns still assume "kick".

## Refactor Scope

Do the larger naming and data-model cleanup now. It is a big win because this
feature changes the workflow from "auction kicker" to "operator action router".

Replace auction-only names with operator/action names:

- `AuctionKicker.sol` -> `TradeHandlerOperator.sol`
- `auction_kicker_address` -> `trade_handler_operator_address`
- `KickPlanner` -> `ActionPlanner`
- `KickExecutor` -> `ActionExecutor`
- `PreparedKick` -> `PreparedAuctionKick`
- `KickPlan` -> `ActionPlan`
- `kick_txs` -> `operator_operations`
- `txn_runs.kicks_*` -> `operator_runs.operations_*`
- `kick_guard_status_latest` -> `source_guard_status_latest`
- `kick_config` -> `action_config`

Keep auction behavior, but make it one operation type:

- `auction_kick`
- `auction_resolve`
- `auction_sweep`
- `auction_enable_tokens`
- `lp_deposit`

Remove the old names instead of aliasing them.

## Target Workflow

The planner should prepare actions in this order:

1. Build the shortlist from scanned balances and thresholds.
2. Apply ignore/cooldown/gauge guards.
3. Run non-auction route adapters against each candidate.
4. Split the candidate set:
   - prepared route actions
   - candidates that did not match any route
   - route-blocked candidates that should not be auctioned
5. Run auction settlement inspection only on the remaining auction candidates.
6. Prepare quote-based auction kicks only for clean auction candidates.
7. Estimate gas and build transaction intents.
8. Execute or dry-run actions and persist results as operator operations.

The important behavior:

- If no route applies, the current auction flow remains the fallback.
- If a route clearly applies but is unsafe or unsupported, skip the candidate
  with a clear reason. Do not silently auction it.
- Route actions do not call the quote API.
- Route actions are not blocked by auction settlement state.
- Route actions can batch with the same route initially. Do not mix auction and
  route operations in one transaction until there is a clear reason.

## Route Adapter Model

Use off-chain route adapters for discovery and preview.

```python
class RouteAdapter(Protocol):
    route_id: bytes
    operation_type: str

    async def inspect(candidate: ActionCandidate) -> RouteInspection:
        ...

    async def prepare(candidate: ActionCandidate, inspection: RouteInspection) -> PreparedRouteAction:
        ...
```

`RouteInspection` should have three outcomes:

- `not_applicable`: continue to the next adapter or auction fallback
- `applicable`: prepare and use the route action
- `blocked`: skip the candidate and do not auction it

This distinction matters. A token that is a coin in the strategy's LP should not
be auctioned just because the LP deposit preview failed or the pool shape is not
yet supported.

Prepared actions should use a common shape:

```python
PreparedRouteAction(
    candidate=...,
    operation_type="lp_deposit",
    route_id=CURVE_LP_DEPOSIT_ROUTE,
    source=...,
    token_in=...,
    amount_in=...,
    want=...,
    route_data=...,
    metadata={...},
)
```

Put route-specific details in `metadata`, not permanent top-level fields. The
same action model should work later for other protocols.

## Contract Model

Replace `AuctionKicker` with one stable `TradeHandlerOperator`.

The operator keeps the current owner/keeper model and hardcoded TradeHandler. It
has two execution surfaces:

1. Built-in typed auction functions for the existing auction workflow.
2. A small route-module registry for non-auction workflows.

The route surface should be stable:

```solidity
struct RouteCall {
    bytes32 routeId;
    address source;
    address tokenIn;
    uint256 amountIn;
    address want;
    bytes data;
}

interface ITradeRoute {
    function build(address tradeHandler, RouteCall calldata call)
        external
        view
        returns (bytes32[] memory commands, bytes[] memory state);
}

function setRoute(bytes32 routeId, address route, bool enabled) external onlyOwner;
function executeRoute(RouteCall calldata call) external onlyKeeperOrOwner;
function batchExecuteRoutes(RouteCall[] calldata calls) external onlyKeeperOrOwner;
```

Why this is simple enough:

- The keeper never supplies raw commands.
- The main operator address stays stable.
- New protocols do not require a new operator deployment or config migration.
- Each route module is a small contract with one job: validate typed data and
  build one TradeHandler command program.
- Existing auction actions stay typed and readable instead of being forced
  through generic route bytes.

Route modules must enforce their own protocol-specific invariants. The operator
only enforces common invariants:

- route is enabled
- `source`, `tokenIn`, `amountIn`, and `want` are nonzero
- only owner/keeper can execute

## First Route: Curve LP Direct Deposit

Implement the first route as `CurveLpDepositRoute`.

Initial scope:

- Strategy and fee-burner sources are allowed if they already grant the
  TradeHandler permission to pull `tokenIn`.
- `want` is the LP token the source should receive.
- The route supports only pool shapes audited for the first target set.
- The route supports only deposit functions that can mint LP tokens directly to
  `source` via a receiver argument.

That last constraint is intentional. If a pool mints LP tokens to the
TradeHandler, the route would need dynamic minted-amount handling before it can
transfer LP tokens back to the source. The current Weiroll helper only builds
simple fixed-argument calls, so receiver-style deposits are the clean first
implementation.

Route data should stay minimal:

```solidity
struct CurveLpDepositData {
    address pool;
    uint8 coinIndex;
    uint8 coinCount;
    uint8 depositKind;
    uint256 minLpOut;
}
```

Validation in the route module:

- `pool` is nonzero.
- `coinIndex < coinCount`.
- `depositKind` is supported.
- `ICurvePool(pool).coins(coinIndex) == call.tokenIn`.
- The pool's LP token/output matches `call.want`, based on the audited pool
  shape.
- The selected deposit function mints to `call.source`.

Command program:

1. `tokenIn.transferFrom(source, tradeHandler, amountIn)`
2. `tokenIn.approve(pool, amountIn)`
3. `pool.add_liquidity(amounts, minLpOut, source)` using the audited selector
4. rely on the audited pool spending the exact approved amount

Do not support mint-to-TradeHandler pool variants in phase 1.
Do not support tokens or pools that leave residual allowance after an exact
single-coin deposit in phase 1.

## Off-Chain LP Detection

The first Tidal adapter should be `CurveLpDepositAdapter`.

Detection:

1. Determine the candidate want:
   - strategy source: read `strategy.want()`
   - fee-burner source: use configured `want_address`
2. Treat `want` as the LP token and, when applicable, the deposit pool.
3. Try bounded `coins(i)` reads for supported pool shapes.
4. If `candidate.token_address` is one of the coins, the route is applicable.
5. Confirm the exact deposit and preview ABI is supported.

For LP token and deposit pool shapes where `want != pool`, use explicit route
metadata from the target audit. Do not guess.

Preview:

1. Read live token balance from the source.
2. Apply existing threshold and sizing policy.
3. Build the single-coin amounts array.
4. Call the supported LP preview method, such as `calc_token_amount`.
5. Apply a small code-default slippage buffer.
6. Encode `CurveLpDepositData`.

Start with a code default of `50 bps` for LP slippage. Do not add a new config
unless real production pools require it.

Blocked outcomes:

- token is an LP coin but deposit ABI is unsupported
- LP preview fails
- preview output is zero
- receiver-style minting is unavailable
- source want does not match the LP output

These should produce clear operator-facing skip reasons.

## Data Model

Replace the auction-shaped `kick_txs` table with a generic operations table.

Suggested shape:

- `id`
- `run_id`
- `operation_type`
- `source_type`
- `source_address`
- `token_in`
- `amount_in`
- `want_address`
- `status`
- `tx_hash`
- `gas_used`
- `gas_price_gwei`
- `block_number`
- `error_message`
- `metadata_json`
- `created_at`
- `updated_at`

Store auction pricing fields and LP deposit details inside `metadata_json`.

Examples:

```json
{
  "auctionAddress": "0x...",
  "startingPrice": "...",
  "minimumPrice": "...",
  "minimumQuote": "...",
  "quoteAmount": "...",
  "quoteResponse": {...}
}
```

```json
{
  "routeId": "curve.lp.deposit.v1",
  "pool": "0x...",
  "coinIndex": 0,
  "coinCount": 2,
  "depositKind": "curve-2coin-receiver",
  "expectedLpOut": "...",
  "minLpOut": "...",
  "slippageBps": 50
}
```

This avoids schema churn for each future route.

## API, CLI, And Service Names

Make the public surface match the new model.

- Rename prepare payloads from kick-only language to action/operator language.
- Rename transaction intent operation values:
  - `kick` -> `auction-kick`
  - `resolve-auction` -> `auction-resolve`
  - `sweep-auction` -> `auction-sweep`
  - `enable-tokens` -> `auction-enable-tokens`
  - `lp-deposit` is new
- Rename config and templates to `trade_handler_operator_address`.
- Rename the service command:
  - `tidal kick run` -> `tidal operator run`
  - `tidal kick prepare` -> `tidal operator prepare`

Because backwards compatibility is not required, do not keep the old commands or
config aliases.

## Target Audit Before Coding

Audit the first target set before writing the route module:

- BOLD-USDC
- DOLA LPs
- yCRV strategies

For each target, record:

- source address and source type
- current `want`
- reward token to deposit
- LP token
- deposit pool or zap
- `coins(i)` outputs
- preview function and selector
- deposit function and selector
- whether receiver-style minting is supported
- whether one-sided deposit is supported
- expected LP token recipient
- whether existing TradeHandler allowance is sufficient

Only targets that satisfy the receiver-style route constraints should be enabled
in the first implementation.

## Tests

Solidity:

- existing auction kick works after the operator rename
- auction resolve/sweep/enable behavior still works
- owner can register and disable a route
- keeper cannot execute disabled or unknown routes
- keeper cannot pass zero source/token/amount/want
- Curve LP route validates `coins(coinIndex) == tokenIn`
- Curve LP route rejects wrong output LP token
- Curve LP route builds a receiver-style deposit that mints LP tokens to source
- batch route execution works for same-route calls
- route never accepts raw keeper-provided commands

Python:

- route adapter prepares an LP deposit when reward token is in LP coins
- LP route preparation does not call quote pricing
- LP route preparation runs before auction settlement inspection
- token not in LP coins falls through to auction prepare
- LP coin with unsupported deposit shape is skipped, not auctioned
- LP preview failure is a clear skip
- tx builder encodes `executeRoute` / `batchExecuteRoutes`
- executor persists `lp_deposit` in the generic operations table
- preview payload includes route metadata
- old config names and old kick-only types are removed

Fork checks:

- run against at least one audited BOLD-USDC or DOLA target
- verify the prepared calldata estimates successfully
- verify LP output recipient is the source, not the TradeHandler

## Implementation Order

1. Audit BOLD-USDC, DOLA LPs, and yCRV targets.
2. Refactor names and schema from kick-only to operator actions.
3. Replace `AuctionKicker` with `TradeHandlerOperator` and port existing auction
   tests.
4. Add the route registry to `TradeHandlerOperator`.
5. Implement `CurveLpDepositRoute` for the smallest audited receiver-style pool
   shape.
6. Add Solidity unit and fork tests for auction behavior and LP route behavior.
7. Add Tidal route adapter infrastructure.
8. Implement `CurveLpDepositAdapter`.
9. Update tx builder, planner, executor, API payloads, CLI output, config
   templates, and docs.
10. Run Python tests, Foundry tests, and one dry-run prepare against each audited
    target family.
11. Deploy the new operator, allowlist it as a TradeHandler mech, register the
    first route module, update server config, and run the operator service with a
    narrow target filter first.

## Summary

The simple version is not "teach the contract about LPs." The simple version is
"Tidal chooses actions; a stable operator executes approved actions."

LP deposits are just the first non-auction action. The first route should be
small and strict: audited Curve-style pools with receiver-style deposits only.
Future protocols get new off-chain adapters and, when necessary, one small
on-chain route module registered on the existing operator.
