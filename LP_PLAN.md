# LP Direct Deposit Plan

## Decision

Build this as a route-aware operator workflow, not as an `AuctionKicker` special
case.

The routing decision belongs in Tidal. The contract should execute a route that
Tidal already selected and validated. It should not discover LPs, inspect
`coins()` loops to choose behavior, call quote APIs, or decide whether to auction.

The clean shape is:

1. Tidal scans and shortlists reward-token candidates as it does today.
2. Tidal asks non-auction route resolvers whether a better direct action exists.
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
- `tidal/transaction_service/evaluator.py` currently applies cached auction
  enabled-token filtering and keeps only one candidate per auction before the
  planner sees the final selected set. Those are auction-only rules and must
  move after route discovery.
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
- `lp_withdraw`

Remove the old names instead of aliasing them.

## Target Workflow

The planner should prepare actions in this order:

1. Build an auction-neutral shortlist from scanned balances and thresholds.
2. Apply global source, ignore, data-freshness, and safety guards.
3. Run non-auction route resolvers against each candidate before any
   auction-only filtering.
4. Split the candidate set:
   - prepared route actions
   - candidates that did not match any route
   - route-blocked candidates that should not be auctioned
5. Apply operation-aware cooldowns to prepared route actions and auction
   fallback candidates.
6. Apply cached auction enabled-token checks, per-auction dedupe, auction
   settlement inspection, and quote preparation only to the remaining auction
   fallback candidates.
7. Estimate gas and build transaction intents.
8. Execute or dry-run actions and persist results as operator operations.

The important behavior:

- If no route applies, the current auction flow remains the fallback.
- If a route clearly applies but is unsafe or unsupported, skip the candidate
  with a clear reason. Do not silently auction it.
- Auction-only filters must not run before route discovery.
- Route actions do not call the quote API.
- Route actions are not blocked by auction settlement state.
- Route actions can batch with the same route initially. Do not mix auction and
  route operations in one transaction until there is a clear reason.

## Route Resolver Model

Use off-chain route resolvers for discovery, preview, min-out calculation, and
route calldata encoding. A resolver is Python code shipped with Tidal. It is not
a contract and it is not a user-created config file.

```python
class RouteResolver(Protocol):
    route_id: bytes
    operation_type: str
    priority: int

    async def resolve(candidate: ActionCandidate, context: RouteContext) -> RouteResolution:
        ...
```

`RouteResolution` should have three outcomes:

- `not_applicable`: continue to the next resolver or auction fallback
- `prepared`: use the returned route action
- `blocked`: skip the candidate and do not auction it

This distinction matters. A token that is a coin in the strategy's LP should not
be auctioned just because the LP deposit preview failed or the pool shape is not
yet supported.

The action planner owns one small `ActionRouter` that runs registered resolvers
in deterministic priority order:

1. The planner builds `ActionCandidate` rows from scanned balances.
2. The planner applies global ignore, threshold, data-freshness, and source
   guards.
3. `ActionRouter` calls each resolver for each candidate.
4. The first prepared route wins.
5. A blocked route stops processing for that candidate and records a skip.
6. Only candidates with no applicable route enter the auction settlement and
   quote-based auction path.

This keeps discovery off-chain while still keeping execution constrained by
audited on-chain route modules.

Suggested code layout after the operator refactor:

- Python resolver protocol and result types:
  `tidal/transaction_service/routes/base.py`
- Python router that runs resolvers before auction preparation:
  `tidal/transaction_service/action_router.py`
- Curve LP resolver implementations:
  `tidal/transaction_service/routes/curve_lp.py`
- Solidity route modules:
  `contracts/src/routes/CurveLpDepositRoute.sol` and
  `contracts/src/routes/CurveLpWithdrawRoute.sol`
- Stable operator contract:
  `contracts/src/TradeHandlerOperator.sol`
- Optional route metadata:
  `config/server.yaml` under `routes`

Operators should not create resolver files or write route logic in config. If a
new protocol family needs support, that is a Tidal code change: add a resolver,
add a route module only if the operator needs new on-chain commands, deploy or
register the module, then add narrow metadata only for audited exceptions.

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
    expected_out=...,
    min_out=...,
    slippage_bps=...,
    preview_block=...,
    route_data=...,
    metadata={...},
)
```

Use the output-bound fields when the route has protocol output risk, such as LP
deposits, LP withdrawals, swaps, mints, redeems, or zaps. Put route-specific
details in `metadata`, not permanent top-level fields. The same action model
should work later for other protocols.

## Route Discovery And Configuration

There are three separate layers:

1. Route capability in code: a route resolver, such as `CurveLpDepositResolver`,
   knows how to detect, preview, and encode one class of action.
2. Optional route metadata in server config: only needed when the resolver cannot
   safely infer pool details from chain reads.
3. On-chain route registration: the operator owner registers the matching route
   module once, such as `curve.lp.deposit.v1 -> CurveLpDepositRoute`.

Do not require manual config for every strategy or every reward token.
Do not require operators to author route code. A new resolver is a Tidal code
change; server config only supplies audited metadata or allowlists for shapes
that cannot be inferred safely.

For the normal BOLD-USDC shape, Tidal should discover the route automatically if
all of these are true:

- the candidate source's `want` is the LP token or pool
- that address exposes the supported `coins(i)` and preview methods
- the reward token is one of the coins
- the supported deposit function can mint LP tokens directly to the source

In that case no per-strategy config entry is needed. The scanner already knows
the source, reward token, balance, auction, and want. The route resolver can read
`want`, probe `coins(i)`, see that BOLD or USDC is an LP coin, preview the
single-sided deposit, and prepare `lp_deposit`.

The inverse should also be discoverable as a separate route: if the candidate
token is the LP token and the source's `want` is one of the LP's coins, Tidal can
prepare an LP withdrawal to that component instead of auctioning the LP token.
That should use a different route resolver, for example `CurveLpWithdrawResolver`,
because preview, min-out, and calldata differ from deposits.

Use config only for non-obvious shapes:

- LP token differs from the deposit pool
- deposits must go through a zap
- a pool has multiple supported deposit ABIs and needs an explicit `depositKind`
- a route should be enabled only for specific sources, wants, or reward tokens
- a protocol needs static metadata that cannot be read reliably on-chain

Keep this config small and declarative. Suggested shape:

```yaml
routes:
  curve_lp_deposit:
    enabled: true
    targets:
      - label: "BOLD-USDC"
        want: "0xLpTokenOrPool"
        pool: "0xDepositPool"
        coin_count: 2
        deposit_kind: "curve-2coin-receiver"
        allowed_tokens:
          - "0xRewardToken"

  curve_lp_withdraw:
    enabled: true
    targets:
      - label: "BOLD-USDC"
        lp_token: "0xLpToken"
        pool: "0xDepositPool"
        coin_count: 2
        withdraw_kind: "curve-2coin-one-coin-receiver"
        allowed_outputs:
          - "0xComponentToken"
```

The config should describe audited exceptions and allowlists. It should not be
the main source of discovery for ordinary pools.

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

## Sibling Route: Curve LP Withdraw To Component

The inverse case should be `CurveLpWithdrawRoute`, not a mode hidden inside
`CurveLpDepositRoute`.

Use it when:

- `call.tokenIn` is the LP token
- `call.want` is one of the LP's underlying coins
- the pool supports a preview method such as `calc_withdraw_one_coin`
- the pool supports a withdraw function that sends the component token directly
  to `call.source`

Route data:

```solidity
struct CurveLpWithdrawData {
    address pool;
    uint8 coinIndex;
    uint8 coinCount;
    uint8 withdrawKind;
    uint256 minTokenOut;
}
```

Validation in the route module:

- `pool` is nonzero.
- `coinIndex < coinCount`.
- `withdrawKind` is supported.
- `ICurvePool(pool).coins(coinIndex) == call.want`.
- the audited pool shape proves `call.tokenIn` is the LP token.
- the selected withdraw function sends the component token to `call.source`.

Command program:

1. `tokenIn.transferFrom(source, tradeHandler, amountIn)`
2. `tokenIn.approve(pool, amountIn)`
3. `pool.remove_liquidity_one_coin(amountIn, coinIndex, minTokenOut, source)`
   using the audited selector
4. rely on the audited pool spending the exact approved amount

Do not support withdraw variants that send output to `msg.sender` in phase 1,
because that would send components to the TradeHandler unless we add dynamic
post-withdraw balance handling.

## Off-Chain LP Route Detection

The first Tidal resolvers should be `CurveLpDepositResolver` and, if target
audits show an immediate inverse opportunity, `CurveLpWithdrawResolver`.

Deposit detection:

1. Determine the candidate want:
   - strategy source: read `strategy.want()`
   - fee-burner source: use configured `want_address`
2. Treat `want` as the LP token and, when applicable, the deposit pool.
3. Try bounded `coins(i)` reads for supported pool shapes.
4. If `candidate.token_address` is one of the coins, the route is applicable.
5. Confirm the exact deposit and preview ABI is supported.

For LP token and deposit pool shapes where `want != pool`, use explicit route
metadata from the target audit. Do not guess.

Withdraw detection:

1. Determine the candidate want.
2. Treat `candidate.token_address` as the possible LP token.
3. Resolve the pool from either the token itself or explicit route metadata.
4. Try bounded `coins(i)` reads for supported pool shapes.
5. If `want` is one of the coins and the candidate token is the LP token, the
   withdraw route is applicable.
6. Confirm the exact withdraw and preview ABI is supported.

Deposit preview:

1. Read live token balance from the source.
2. Apply existing threshold and sizing policy.
3. Build the single-coin amounts array.
4. Call the supported LP preview method, such as `calc_token_amount`.
5. Compute `minLpOut` with the route's min-out policy.
6. Encode `CurveLpDepositData`.

Withdraw preview:

1. Read live LP token balance from the source.
2. Apply existing threshold and sizing policy.
3. Call the supported LP withdraw preview, such as
   `calc_withdraw_one_coin(amountIn, coinIndex)`.
4. Compute `minTokenOut` with the route's min-out policy.
5. Encode `CurveLpWithdrawData`.

Blocked outcomes:

- token is an LP coin but deposit ABI is unsupported
- token is the LP token but withdraw ABI is unsupported
- LP preview fails
- preview output is zero
- receiver-style minting is unavailable
- receiver-style component withdrawal is unavailable
- source want does not match the LP output
- source want is not an LP coin for the withdraw route

These should produce clear operator-facing skip reasons.

## Min-Out And Public RPC Safety

Any route that receives protocol output must set an explicit output bound before
Tidal broadcasts through public RPC.

This applies to LP deposits, LP withdrawals, swaps, zaps, mints, redeems, and any
future route where public mempool execution can land after unfavorable state
changes.

Each route resolver owns a small min-out policy:

1. Read the protocol preview from the same chain state used for prepare.
2. Record the preview method and preview block in action metadata.
3. Compute route-specific price impact when the protocol exposes enough data.
4. Set `slippage_bps = max(route_default_bps, impact_bps + safety_bps)`.
5. Cap `slippage_bps` with a route-specific maximum.
6. Compute `min_out = expected_out * (10_000 - slippage_bps) / 10_000`.
7. Block the route if `expected_out == 0`, `min_out == 0`, preview data is stale,
   or required preview data is unavailable.

Start with code defaults, not user config:

- Curve LP deposit default: `50 bps`
- route max: set during target audit
- preview max age: reuse `prepared_action_max_age_seconds`

Only add operator config after real production routes prove the defaults need to
vary by deployment.

The transaction builder must put `min_out` inside route calldata. A route module
must never use `0` as a placeholder min-out unless the route proves in code and
tests that the output amount is invariant.

Execution should reject stale prepared actions. If an action is too old, Tidal
should re-read live balance, re-preview expected output, recompute `min_out`, and
rebuild calldata before signing.

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
  "slippageBps": 50,
  "previewBlock": 12345678,
  "previewMethod": "calc_token_amount"
}
```

This avoids schema churn for each future route.

## Logs And Web Visibility

Every operator action must write a database log row, including new route actions.

`operator_operations` is the source of truth for:

- CLI logs
- API logs
- the web app `/logs` view
- run detail pages
- failure summaries

The API read model should be renamed from kick logs to operator operation logs
and should return all operation types in one timeline. The web app should keep a
single `/logs` view, but it should no longer assume every row is an auction kick.

Required log behavior:

- `lp_deposit` rows appear in `/logs` alongside auction actions.
- `lp_withdraw` rows appear in `/logs` alongside auction actions.
- filters work across status, source, token, transaction hash, run id, and
  operation type.
- search includes route metadata where useful, such as pool, route id, and
  deposit kind.
- each row shows a readable operation label and route-specific detail summary.
- the detail drawer can render `metadata_json` for route actions.
- Auctionscan fields and matching remain auction-action-only.
- route actions without an auction address must still render cleanly.

Do not keep a separate route-log path. The operator log is one shared operation
timeline.

## API, CLI, And Service Names

Make the public surface match the new model.

- Rename prepare payloads from kick-only language to action/operator language.
- Rename `/api/v1/tidal/logs/kicks` to an operator-log endpoint.
- Keep the web app route `/logs`, but wire it to the operator-log endpoint.
- Rename transaction intent operation values:
  - `kick` -> `auction-kick`
  - `resolve-auction` -> `auction-resolve`
  - `sweep-auction` -> `auction-sweep`
  - `enable-tokens` -> `auction-enable-tokens`
  - `lp-deposit` is new
  - `lp-withdraw` is new
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
- withdraw preview function and selector
- withdraw function and selector
- whether receiver-style minting is supported
- whether receiver-style component withdrawal is supported
- whether one-sided deposit is supported
- whether one-sided withdrawal is supported
- expected LP token recipient
- expected component token recipient
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
- Curve LP withdraw route validates `coins(coinIndex) == want`
- Curve LP withdraw route rejects wrong input LP token
- Curve LP withdraw route builds a receiver-style one-coin withdrawal that sends
  the component token to source
- batch route execution works for same-route calls
- route never accepts raw keeper-provided commands

Python:

- route resolver prepares an LP deposit when reward token is in LP coins
- route resolver prepares an LP withdrawal when reward token is the LP token and
  `want` is one of the LP coins
- LP route preparation does not call quote pricing
- LP route preparation runs before auction settlement inspection
- LP route discovery runs before cached auction enabled-token filtering
- LP route discovery sees multiple same-auction candidates before auction
  dedupe
- auction kick cooldowns do not block route actions unless an operation-aware
  route cooldown rule explicitly matches
- LP route computes nonzero `expected_out` and `min_out`
- LP route records preview block, preview method, and slippage bps
- stale LP route previews are rejected and re-prepared before signing
- token not in LP coins falls through to auction prepare
- LP coin with unsupported deposit shape is skipped, not auctioned
- LP token with unsupported withdraw shape is skipped, not auctioned
- LP preview failure is a clear skip
- tx builder encodes `executeRoute` / `batchExecuteRoutes`
- executor persists `lp_deposit` in the generic operations table
- executor persists `lp_withdraw` in the generic operations table when the
  withdraw route is enabled
- preview payload includes route metadata
- API logs return `lp_deposit` rows from the generic operations table
- API logs return `lp_withdraw` rows from the generic operations table when the
  withdraw route is enabled
- web `/logs` renders route actions and auction actions in one timeline
- Auctionscan lookup ignores non-auction route actions
- old config names and old kick-only types are removed

Fork checks:

- run against at least one audited BOLD-USDC or DOLA target
- verify the prepared calldata estimates successfully
- verify LP output recipient is the source, not the TradeHandler
- verify component output recipient is the source, not the TradeHandler
- verify calldata contains a nonzero public-RPC-safe `minLpOut`
- verify withdraw calldata contains a nonzero public-RPC-safe `minTokenOut`

## Implementation Order

1. Audit BOLD-USDC, DOLA LPs, and yCRV targets.
2. Refactor names and schema from kick-only to operator actions.
3. Split candidate selection into an auction-neutral balance shortlist plus
   auction-only fallback filters.
4. Replace `AuctionKicker` with `TradeHandlerOperator` and port existing auction
   tests.
5. Add the route registry to `TradeHandlerOperator`.
6. Implement `CurveLpDepositRoute` for the smallest audited receiver-style pool
   shape. Add `CurveLpWithdrawRoute` in the same phase if the target audit shows
   an immediate LP-token-to-component opportunity.
7. Add Solidity unit and fork tests for auction behavior and LP route behavior.
8. Add Tidal route resolver infrastructure.
9. Implement `CurveLpDepositResolver`. Add `CurveLpWithdrawResolver` in the same
   phase if the route module is included.
10. Add generic operation logging, API log reads, and web `/logs` rendering for
   all operation types.
11. Update tx builder, planner, executor, API payloads, CLI output, config
   templates, and docs.
12. Run Python tests, Foundry tests, and one dry-run prepare against each audited
    target family.
13. Deploy the new operator, allowlist it as a TradeHandler mech, register the
    first route module, update server config, and run the operator service with a
    narrow target filter first.

## Summary

The simple version is not "teach the contract about LPs." The simple version is
"Tidal chooses actions; a stable operator executes approved actions."

LP deposits are just the first non-auction action. The inverse LP-token to
component-token flow is the same architecture but a separate withdraw route. The
first routes should stay small and strict: audited Curve-style pools with
receiver-style deposits or withdrawals only. Future protocols get new off-chain
resolvers and, when necessary, one small on-chain route module registered on the
existing operator.
