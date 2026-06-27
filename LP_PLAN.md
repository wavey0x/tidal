# LP Direct Deposit Plan

## Goal

Tidal chooses actions; a stable operator contract executes approved actions.

LP deposits are the first non-auction action. Today the only action is "kick an
auction." This plan generalizes the workflow to an action router so a candidate
reward token can be deposited straight into its strategy's LP instead of being
auctioned, with auction kicks remaining the fallback when no better route exists.

The routing decision lives in Tidal. The contract executes a route that Tidal
already selected and validated; it never discovers LPs, loops over `coins()`,
calls quote APIs, or decides whether to auction.

End-to-end shape:

1. Tidal builds an auction-neutral shortlist from scanned balances (as today).
2. Off-chain route resolvers inspect each candidate. The first applicable route
   wins and removes the candidate from the auction path.
3. Candidates with no applicable route fall through to the existing auction kick
   path, unchanged.
4. One `TradeHandlerOperator` contract executes either typed auction actions or
   an audited route module.

This gives us LP deposits now and a clean home for future non-auction
opportunities without one-off config, duplicate contracts, or a new database
column per route.

## Non-Goals

- No `if rewardToken in lp.coins()` branching inside the auction kick path.
- No arbitrary keeper-supplied Weiroll commands.
- No attempt to support every Curve pool or every LP deposit ABI in phase 1.
- No prices or quote APIs for LP routes.
- No backwards compatibility. Old config names, contract names, endpoints, CLI
  commands, and code paths are removed, not aliased. Breaking changes are fine.

## Architecture

Three layers, each with one job:

- **Off-chain resolvers (Python, shipped with Tidal).** Discover, preview,
  compute min-out, and encode route calldata. A resolver is code, not a contract
  and not an operator-authored config file.
- **Action router + planner (Python).** Build candidates, apply global guards,
  run resolvers in priority order, then hand unmatched candidates to the existing
  auction path.
- **Operator contract + route modules (Solidity).** `TradeHandlerOperator` keeps
  the typed auction functions and adds a small registry of audited route modules.
  Each route module validates typed data and builds one TradeHandler command
  program.

Discovery stays off-chain and flexible; execution stays constrained by audited
on-chain modules. The keeper never supplies raw commands.

## Naming And Schema Refactor

This feature reframes "auction kicker" as "operator action router," so do the
rename and data-model cleanup now. Remove old names; do not alias.

| Old | New |
| --- | --- |
| `AuctionKicker.sol` | `TradeHandlerOperator.sol` |
| `auction_kicker_address` (config + templates) | `trade_handler_operator_address` |
| `KickPlanner` | `ActionPlanner` |
| `KickExecutor` | `ActionExecutor` |
| `KickTxBuilder` | `ActionTxBuilder` |
| `KickTxRepository` | `OperatorOperationRepository` |
| `PreparedKick` | `PreparedAuctionKick` |
| `KickPlan` | `ActionPlan` |
| `kick_txs` table | `operator_operations` table |
| `txn_runs.kicks_*` | `operator_runs.operations_*` |
| `kick_guard_status_latest` | `source_guard_status_latest` |
| `kick_config` | `action_config` |

Auction behavior is preserved but becomes one of several `operation_type` values:

`auction_kick`, `auction_resolve`, `auction_sweep`, `auction_enable_tokens`,
`lp_deposit`, and (when built) `lp_withdraw`.

### Data model

Replace the auction-shaped `kick_txs` table with a generic operations table, but
do **not** blindly collapse existing auction columns into JSON. The current
raw-SQL log model, dashboard reads, cooldown lookups, and Auctionscan poller read
and update several auction fields as ordinary columns. Moving those into
`metadata_json` would force `json_extract` into hot SELECT/WHERE/ORDER paths and
would lose simple index efficiency.

Use a hybrid schema:

- Keep stable query, display, cooldown, and poller fields as nullable columns.
- Put route-specific or rarely queried detail in `metadata_json`.
- Promote a metadata field to a real column later if it becomes a filter,
  ordering key, poller key, or high-volume lookup.

```
operator_operations(
  id, run_id, chain_id, operation_type,
  source_type, source_address,
  auction_address,
  token_in, token_symbol, amount_in, normalized_amount_in,
  price_usd, usd_value,
  want_address, want_symbol,
  starting_price, minimum_price, minimum_quote,
  quote_amount, quote_response_json,
  start_price_buffer_bps, min_price_buffer_bps, step_decay_rate_bps,
  settle_token, stuck_abort_reason,
  auctionscan_round_id, auctionscan_last_checked_at, auctionscan_matched_at,
  status, tx_hash, gas_used, gas_price_gwei, block_number,
  error_message, metadata_json,
  created_at, updated_at
)
```

The auction columns stay nullable so non-auction route rows render cleanly.
`auction_address` is set for auction actions and may be set for route rows only
when there is a useful candidate auction context.

`metadata_json` is still useful, but its role is narrower: it stores route
details such as pool, coin index, expected/min out, preview block, and
route-specific audit data. It can also store verbose auction payloads that are
not used by logs, filters, cooldowns, or Auctionscan polling.

Carry forward equivalent column indexes for the existing hot paths:

- created-at and status-created log queries
- source/token and auction/token cooldown lookups
- run detail lookups
- Auctionscan pending-poll queries over confirmed auction kicks with null
  `auctionscan_round_id`

Do not implement Auctionscan polling through JSON extraction.

Auction kick metadata can be small because the common auction fields above remain
columns:

```json
{ "quoteResponseSummary": {}, "pricingProfileName": "volatile" }
```

LP deposit metadata:

```json
{ "routeId": "curve.lp.deposit.v1", "pool": "0x...", "coinIndex": 0,
  "coinCount": 2, "depositKind": "curve-2coin-receiver",
  "expectedLpOut": "...", "minLpOut": "...", "slippageBps": 50,
  "previewBlock": 12345678, "previewMethod": "calc_token_amount" }
```

## Planner Workflow

The current code applies auction-only filtering too early:
`evaluator.build_shortlist` calls `_filter_by_cached_auction_enablement` and
`_best_candidate_per_auction` *inside* the shortlist, and `KickPlanner.plan`
groups candidates by auction and inspects settlement before prepare. Those rules
are wrong for routes: an LP deposit does not care whether the auction is idle,
live, or dirty, and it must see every same-auction candidate, not one per
auction.

Reorder so route discovery runs on the full, auction-neutral candidate set:

1. **Auction-neutral shortlist.** `build_shortlist` returns candidates above
   threshold with fresh data. Remove ignore, cooldown,
   `_filter_by_cached_auction_enablement`, `_best_candidate_per_auction`, and
   operation limits from this function.
2. **Global guards.** Apply ignore, threshold, data-freshness, and source
   (killed-gauge) guards. These are route-agnostic and stay here.
3. **Route discovery.** `ActionRouter` runs resolvers against every candidate.
   Split the result into: prepared route actions, route-blocked candidates
   (skipped with a reason), and unmatched candidates.
4. **Cooldowns and limits.** Apply operation-aware cooldowns and requested action
   limits to prepared route actions and unmatched auction fallback candidates.
5. **Auction fallback.** Apply the auction-only rules — now moved here — to
   unmatched candidates only: cached enabled-token filter, per-auction dedupe,
   settlement inspection, and quote-based kick prepare.
6. **Build and execute.** Estimate gas, build `TxIntent`s, execute or dry-run,
   and persist every action as an `operator_operations` row.

Invariants:

- No applicable route → existing auction flow runs unchanged.
- Route clearly applies but is unsafe/unsupported → skip with a clear reason;
  never silently auction it.
- Route actions never call the quote API and are never blocked by auction
  settlement state.
- Route actions may batch with the same route. Do not mix auction and route
  operations in one transaction in phase 1.

`ActionPlan` mirrors today's `KickPlan`: typed lists per operation family plus a
shared `tx_intents` list and skip/warning collections.

## Route Resolvers (Off-Chain)

```python
class RouteResolver(Protocol):
    route_id: bytes            # 32-byte keccak256("curve.lp.deposit.v1")
    operation_type: str        # "lp_deposit"
    priority: int

    async def resolve(self, candidate: ActionCandidate, ctx: RouteContext) -> RouteResolution:
        ...
```

`RouteResolution` is one of three outcomes:

- `not_applicable` — try the next resolver, else auction fallback.
- `prepared` — use the returned `PreparedRouteAction`. First prepared wins.
- `blocked` — skip the candidate and record a reason. **Do not auction it.**

The `blocked` outcome is the important one: a token that *is* a coin in the
strategy's LP should not be auctioned just because the deposit preview failed or
the pool shape is unsupported.

`ActionRouter` runs registered resolvers in deterministic priority order, calling
each resolver for each candidate until one returns `prepared` or `blocked`.

Prepared actions share one shape; route-specific fields go in `metadata`, not new
top-level columns:

```python
PreparedRouteAction(
    candidate, operation_type, route_id, source,
    token_in, amount_in, want,
    expected_out, min_out, slippage_bps,   # required for any output-bearing route
    preview_block, route_data, metadata={...},
)
```

Use the output-bound fields (`expected_out`/`min_out`/`slippage_bps`) for any
route with protocol output risk: LP deposits and withdrawals, swaps, zaps, mints,
redeems. See "Min-Out And Public RPC Safety."

### Code layout

- `tidal/transaction_service/routes/base.py` — protocol + result types
- `tidal/transaction_service/action_router.py` — runs resolvers before fallback
- `tidal/transaction_service/routes/curve_lp.py` — Curve resolvers
- `contracts/src/TradeHandlerOperator.sol` — stable operator
- `contracts/src/routes/CurveLpDepositRoute.sol` — first route module
- `config/server.yaml` under `routes` — optional, audited metadata only

## Operator Contract And Route Modules (On-Chain)

`TradeHandlerOperator` replaces `AuctionKicker`, keeping the owner/keeper model
and hardcoded TradeHandler. It has two surfaces: the existing typed auction
functions (kept readable, not forced through generic bytes) and a route registry:

```solidity
struct RouteCall {
    bytes32 routeId;
    address source;
    address tokenIn;
    uint256 amountIn;
    address want;
    bytes data;       // abi-encoded route-specific struct
}

interface ITradeRoute {
    function build(address tradeHandler, RouteCall calldata call)
        external view returns (bytes32[] memory commands, bytes[] memory state);
}

function setRoute(bytes32 routeId, address route, bool enabled) external onlyOwner;
function executeRoute(RouteCall calldata call) external onlyKeeperOrOwner;
function batchExecuteRoutes(RouteCall[] calldata calls) external onlyKeeperOrOwner;
```

The operator enforces only common invariants: route is enabled; `source`,
`tokenIn`, `amountIn`, `want` are nonzero; caller is owner/keeper. Each route
module enforces its own protocol-specific invariants. New protocols add a module
and a `setRoute` call — no operator redeploy, no config migration.

### WeiRoll constraint (drives the receiver-style requirement)

`WeiRollCommandLib.cmdCall` builds fixed-argument calls with an **unused output
slot** — it cannot chain a dynamic return value (such as a minted LP amount) into
a later command. So phase-1 routes must use deposit/withdraw functions that send
output **directly to `source`** via a receiver argument. Mint-to-operator
variants would require dynamic post-call balance handling and are out of scope.

### First route: `CurveLpDepositRoute`

```solidity
struct CurveLpDepositData {
    address pool;
    uint8 coinIndex;
    uint8 coinCount;
    uint8 depositKind;
    uint256 minLpOut;
}
```

Module validation:

- `pool != 0`, `coinIndex < coinCount`, `depositKind` supported.
- `ICurvePool(pool).coins(coinIndex) == call.tokenIn`.
- The pool's LP output matches `call.want` for the audited shape.
- The selected deposit function mints to `call.source`.

Command program:

1. `tokenIn.transferFrom(source, tradeHandler, amountIn)`
2. `tokenIn.approve(pool, amountIn)`
3. `pool.add_liquidity(amounts, minLpOut, source)` (audited selector)

Phase-1 exclusions: no mint-to-operator pools; no tokens/pools that leave
residual allowance after an exact single-coin deposit.

### Sibling route: `CurveLpWithdrawRoute` (symmetric, build when a target needs it)

The LP-token → component case is the exact mirror of deposit, as a *separate*
route module (preview, min-out, and calldata differ). Build it the same way,
swapping these specifics:

- `call.tokenIn` is the LP token; `call.want` is a component coin.
- Validate `ICurvePool(pool).coins(coinIndex) == call.want` and that
  `call.tokenIn` is the LP token for the audited shape.
- Command 3 becomes
  `pool.remove_liquidity_one_coin(amountIn, coinIndex, minTokenOut, source)`.
- Data struct mirrors deposit with `withdrawKind` and `minTokenOut`.
- Same receiver-style requirement: output must go to `source`, not the operator.

Treat withdraw as phase 2 unless the target audit surfaces an immediate
LP-token-to-component opportunity, in which case build it alongside deposit.

## Off-Chain LP Route Detection

For the normal BOLD-USDC shape, the deposit route is fully discoverable from
chain reads — no per-strategy config:

1. Read the candidate `want` (strategy: `strategy.want()`; fee-burner: configured
   `want_address`). Treat it as the LP token / deposit pool.
2. Bounded `coins(i)` reads for supported pool shapes.
3. If `candidate.token_address` is one of the coins, the route applies.
4. Confirm the exact deposit + preview ABI is supported and receiver-style.

Withdraw detection is the mirror: treat the candidate token as the LP token,
resolve the pool, and apply if `want` is one of the pool's coins.

Deposit preview:

1. Read live token balance from the source; apply existing threshold/sizing.
2. Build the single-coin amounts array.
3. Call the supported preview (`calc_token_amount`).
4. Compute `minLpOut` via the route's min-out policy.
5. Encode `CurveLpDepositData`.

Withdraw preview mirrors this using `calc_withdraw_one_coin(amountIn, coinIndex)`.

Blocked outcomes (each a clear operator-facing skip reason):

- token is an LP coin/LP token but the deposit/withdraw ABI is unsupported
- preview fails or returns zero
- receiver-style mint/withdraw is unavailable
- `want` does not match the LP output (deposit) or is not an LP coin (withdraw)

Use config only for shapes that cannot be inferred safely (see Configuration).
Do not guess.

## Min-Out And Public RPC Safety

Any route that receives protocol output must set an explicit output bound before
Tidal broadcasts through public RPC. Each resolver owns a small min-out policy:

1. Read the protocol preview from the same chain state used to prepare; record
   the preview method and block in metadata.
2. `slippage_bps = max(route_default_bps, impact_bps + safety_bps)`, capped at a
   route maximum.
3. `min_out = expected_out * (10_000 - slippage_bps) / 10_000`.
4. Block if `expected_out == 0`, `min_out == 0`, or preview data is stale/missing.

Start with code defaults, not operator config:

- Curve LP deposit default: `50 bps`
- route max: set during the target audit
- preview max age: reuse `prepared_action_max_age_seconds` (currently 300)

The tx builder puts `min_out` inside route calldata. A route module must never use
`0` as a placeholder min-out unless it proves in code and tests that the output is
invariant. Execution rejects stale prepared actions: if too old, re-read balance,
re-preview, recompute `min_out`, and rebuild calldata before signing.

## Configuration

Three independent layers:

1. **Capability in code** — a resolver knows how to detect/preview/encode a class
   of action.
2. **Optional metadata in `server.yaml`** — only when the resolver cannot safely
   infer pool details from chain reads.
3. **On-chain registration** — the owner calls `setRoute(routeId, module, true)`
   once.

Operators never author resolver code or route logic in config. Config supplies
audited metadata and allowlists only — for non-obvious shapes:

- LP token differs from the deposit pool
- deposits must go through a zap
- a pool has multiple deposit ABIs and needs an explicit `depositKind`
- a route should be limited to specific sources, wants, or reward tokens
- static metadata that cannot be read reliably on-chain

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
        allowed_tokens: ["0xRewardToken"]
```

Withdraw config mirrors this (`lp_token`, `withdraw_kind`, `allowed_outputs`).
Keep config small and declarative; it describes exceptions, not ordinary pools.

## Logs And Surfaces

`operator_operations` is the single source of truth for CLI logs, API logs, the
web `/logs` view, run detail pages, and failure summaries. Every action — auction
or route — writes a row. There is no separate route-log path.

- Rename the API read model from "kick logs" to operator-operation logs; return
  all operation types in one timeline.
- Rename `/api/v1/tidal/logs/kicks` to an operator-log endpoint; keep the web
  route `/logs` but wire it to the new endpoint.
- `/logs` filters work across status, source, token, tx hash, run id, and
  operation type; search includes useful route metadata (pool, route id, deposit
  kind). The detail drawer renders `metadata_json` for route rows.
- `lp_deposit` / `lp_withdraw` rows render cleanly with no auction address.
  Auctionscan fields and matching stay auction-only.
- Auctionscan read/update paths continue to use first-class columns, not
  `metadata_json`.

Rename the public surface to match the model:

- transaction intent operations: `kick`→`auction-kick`, `resolve-auction`→
  `auction-resolve`, `sweep-auction`→`auction-sweep`, `enable-tokens`→
  `auction-enable-tokens`; `lp-deposit` / `lp-withdraw` are new.
- CLI: `tidal kick run`→`tidal operator run`,
  `tidal kick prepare`→`tidal operator prepare`.
- config + templates: `trade_handler_operator_address`.

No old commands or config aliases are kept.

## Target Audit Before Coding

Audit the first target set — BOLD-USDC, DOLA LPs, yCRV strategies — before
writing the route module. For each target record:

- source address + type, current `want`, reward token, LP token, deposit
  pool/zap
- `coins(i)` outputs
- deposit/withdraw preview functions + selectors
- deposit/withdraw functions + selectors
- whether receiver-style mint and receiver-style component withdrawal exist
- whether one-sided deposit / withdrawal is supported
- expected LP and component recipients
- whether existing TradeHandler allowance is sufficient
- chosen route max slippage bps

Only targets that satisfy the receiver-style constraint are enabled in phase 1.

## Tests

Solidity:

- existing auction kick / resolve / sweep / enable still work after the rename
- owner can register and disable a route; keeper cannot execute a disabled or
  unknown route
- operator rejects zero source/token/amount/want and raw keeper-provided commands
- `CurveLpDepositRoute`: validates `coins(coinIndex) == tokenIn`, rejects wrong
  output LP token, builds a receiver-style deposit that mints LP to `source`
- batch execution works for same-route calls
- (with withdraw) `CurveLpWithdrawRoute`: validates `coins(coinIndex) == want`,
  rejects wrong input LP token, sends the component to `source`

Python:

- resolver prepares an `lp_deposit` when the reward token is an LP coin
- LP route prep does not call quote pricing and is not gated by auction
  settlement
- route discovery runs before cached enabled-token filtering and before
  per-auction dedupe (sees multiple same-auction candidates)
- auction cooldowns do not block route actions unless an operation-aware route
  cooldown matches
- LP route computes nonzero `expected_out`/`min_out` and records preview block,
  method, and slippage bps; stale previews are re-prepared before signing
- token not in LP coins falls through to auction prepare; LP coin with
  unsupported shape and LP preview failure are skips, not auctions
- tx builder encodes `executeRoute` / `batchExecuteRoutes`
- executor persists `lp_deposit` in `operator_operations`; API logs and web
  `/logs` render route and auction actions in one timeline
- Auctionscan lookup ignores non-auction rows and continues to query/update
  column fields, not JSON metadata
- old config names and old kick-only operation types are gone

Fork checks (run against an audited BOLD-USDC or DOLA target):

- prepared calldata estimates successfully
- LP output recipient is `source`, not the TradeHandler
- calldata contains a nonzero, public-RPC-safe `minLpOut`

## Implementation Order

1. Audit BOLD-USDC, DOLA LPs, and yCRV targets.
2. Rename names and schema from kick-only to operator actions; migrate
   `kick_txs` into `operator_operations` with a hybrid schema: preserve hot
   auction/log/poller columns, add `metadata_json` for route-specific detail.
3. Move auction-only filters out of `build_shortlist` and into the auction
   fallback step; split selection into auction-neutral shortlist + fallback.
4. Replace `AuctionKicker` with `TradeHandlerOperator`; port auction tests.
5. Add the route registry to `TradeHandlerOperator`.
6. Implement `CurveLpDepositRoute` for the smallest audited receiver-style shape.
7. Add Solidity unit + fork tests for auction and LP deposit behavior.
8. Add the Python resolver protocol + `ActionRouter`.
9. Implement `CurveLpDepositResolver`.
10. Add generic operation logging, API log reads, and web `/logs` rendering for
    all operation types.
11. Update tx builder, planner, executor, API payloads, CLI, config templates,
    docs.
12. Run Python + Foundry tests and one dry-run prepare per audited target family.
13. Deploy the operator, allowlist it as a TradeHandler mech, register the first
    route, update server config, run with a narrow target filter first.
14. (Optional, when a target needs it) build `CurveLpWithdrawRoute` and
    `CurveLpWithdrawResolver` following the deposit pattern.
