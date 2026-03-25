# Fee Burner Integration Plan

## Scope

Add the Yearn fee burner at `0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8` as a first-class sell source across:

- on-chain kicking via `contracts/src/AuctionKicker.sol`
- scanner + persistence + txn service in `factory_dashboard/`
- read-only dashboard API that lives outside this repo
- UI with a dedicated `Fee Burner` tab, while keeping `Strategies` vault/strategy-centric

## Confirmed On-Chain Facts

- `0xb911...1eE8` is a verified `FeeBurner` contract, not a strategy.
- It does not expose `want()` or `vault()`.
- `tradeHandler` `0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b` is already an approved spender on the fee burner.
- `getApprovals(tradeHandler)` currently returns 48 approved tokens.
- The auction factory currently has exactly one valid auction with `receiver == 0xb911...1eE8`:
  - auction: `0x10Bd77b0aA255d5cb7db1273705C1f0568536035`
  - want: `0xf939e0a03fb07f59a73314e73794be0e57ac1b4e` (`crvUSD`)
  - version: `1.0.3cc`

## Design Decisions

### 1. Separate UI tabs, shared source model underneath

- Keep `Strategies` focused on vault + strategy rows.
- Add a dedicated `Fee Burner` tab instead of forcing the strategy table to become a generic catch-all.
- Keep `Kick Log` cross-source and rename its strategy-specific labels to source-neutral labels.

### 2. Do not fake the fee burner as a strategy

- Do not put the fee burner into `ADDITIONAL_DISCOVERY_STRATEGIES`.
- Do not create a fake vault row just to satisfy the current UI.
- Do not rely on `strategy.want()` anywhere for fee burner support.

### 3. Generalize contract and txn interfaces, not every storage table

- Make the contract and transaction path source-neutral.
- Keep existing `strategies` / `vaults` tables for real strategies.
- Add additive fee burner persistence instead of renaming the entire schema to `sources` in this change.

### 4. Use explicit fee burner registration

- The fee burner should be explicitly configured/registered.
- Recommended: config-driven list in `config.yaml` + `Settings`.
- Acceptable fallback if scope must stay smaller: a constant registry in `factory_dashboard/constants.py`.

### 5. Preserve backward compatibility during rollout

- External API should emit new generic `source*` fields and retain legacy strategy fields for one coordinated deploy window.
- `kick_txs` should gain `source_type` / `source_address` via additive migration before any cleanup.

## Recommended Target Model

### Logical source model

Every sellable entity exposed to the UI/API/txn path should be representable as:

- `sourceType`: `strategy` or `fee_burner`
- `sourceAddress`
- `sourceName`
- `contextType`: `vault` or `null`
- `contextAddress`
- `contextName`
- `contextSymbol`
- `auctionAddress`
- `auctionVersion`
- `wantAddress`
- `wantSymbol`
- `balances`
- `kicks`
- `scannedAt`

### Strategy rows

- `sourceType = "strategy"`
- `sourceAddress = strategy address`
- `sourceName = strategy name`
- `contextType = "vault"`
- `context* = vault metadata`

### Fee burner rows

- `sourceType = "fee_burner"`
- `sourceAddress = fee burner address`
- `sourceName = "Yearn Fee Burner"` or configured label
- `contextType = null`
- no fake vault fields

## Contract Plan

### Goals

- `AuctionKicker` must support any source address that can be the `receiver` for an auction and can be pulled from via `tradeHandler`.
- Validation must remain strict enough to prevent misconfigured auctions.

### Changes

- Generalize `strategy` naming to `source` inside `contracts/src/AuctionKicker.sol`.
- Rename `KickParams.strategy` to `KickParams.source`.
- Add `wantToken` to `KickParams`.
- Replace `require(auction.want() == IStrategy(source).want())` with:
  - `require(auction.want() == wantToken, "want mismatch")`
- Keep:
  - `require(auction.receiver() == source, "receiver mismatch")`
- Keep the same 4-step Weiroll program.
- Keep `kick()` and `batchKick()` entrypoints, but change the tuple shape to include `wantToken`.
- Rename `Kicked(address indexed strategy, ...)` to `Kicked(address indexed source, ...)` and rename any remaining event fields from strategy-centric naming to source-centric naming.

### Why this shape

- Strategies can still pass their `want` token.
- Fee burner sources can pass `want` derived from the auction.
- The contract no longer assumes the source has a `want()` function.

## Persistence Plan

### Recommended additive schema

Keep existing strategy tables untouched and add fee burner-specific tables:

- `fee_burners`
- `fee_burner_tokens`
- `fee_burner_token_balances_latest`

Recommended columns:

`fee_burners`
- `address`
- `chain_id`
- `name`
- `active`
- `auction_address`
- `want_address`
- `auction_version`
- `auction_updated_at`
- `auction_error_message`
- `first_seen_at`
- `last_seen_at`

`fee_burner_tokens`
- `fee_burner_address`
- `token_address`
- `source`
  - v1 value: `"trade_handler_approval"`
- `active`
- `first_seen_at`
- `last_seen_at`

`fee_burner_token_balances_latest`
- `fee_burner_address`
- `token_address`
- `raw_balance`
- `normalized_balance`
- `block_number`
- `scanned_at`

### Generic kick history

Add to `kick_txs`:

- `source_type`
- `source_address`
- optional `source_name` if the external API wants to avoid joining back for labels

Migration order matters here because existing `kick_txs.strategy_address` rows are populated and `strategy_address` is currently non-null:

1. add `source_type` and `source_address` as nullable columns
2. backfill existing rows
3. optionally tighten the new columns to `NOT NULL` in the same migration or an immediate follow-up once backfill succeeds

Backfill existing rows with:

- `source_type = "strategy"`
- `source_address = strategy_address`

Keep `strategy_address` for now to minimize migration risk and to support temporary API/UI compatibility.

### Optional but recommended cleanup

Add source-neutral fields to `scan_item_errors`:

- `source_type`
- `source_address`

This is not mandatory for first functional rollout, but it keeps diagnostics honest once fee burner scan failures exist.

## Scanner Plan

### High-level flow

Keep the existing strategy scan flow. Add a second explicit fee burner scan path, then merge both into shared downstream steps where practical:

1. Discover strategies as today.
2. Refresh strategy auction mappings as today.
3. Discover configured fee burners.
4. Read fee burner approved tokens via `getApprovals(tradeHandler)`.
5. Refresh token metadata and prices for the union of strategy tokens and fee burner tokens.
6. Read balances for both strategies and fee burners.
7. Persist strategy balances to strategy tables and fee burner balances to fee burner tables.

For scan accounting, keep v1 intentionally simple:

- do not add fee-burner-specific columns to `scan_runs`
- let existing `pairs_*` counters continue representing aggregate scanned pairs across all sources
- if burner-specific counts are useful, emit them in logs first rather than expanding schema

### Fee burner registration source of truth

YAML registration should define fee burner identity and expected want:

- YAML decides which fee burner addresses are monitored.
- YAML also declares the expected `want_address` for each fee burner.
- On-chain reads remain the source of truth for:
  - approved sell tokens via `getApprovals(tradeHandler)`
  - auction address
  - auction version
- Do not store token allowlists, spender addresses, or resolved auction metadata in YAML.

Recommended semantics:

- required field: `address`
- required field: `want_address`
- optional field: `label`
- removing an entry from YAML means "stop monitoring this burner"
- default behavior when omitted: no fee burners are monitored

### Auction mapping for fee burner

Fee burner mapping should not use the strategy `want()` path.

Recommended behavior:

- Reuse the auction metadata snapshot already fetched from the auction factory.
- For each configured fee burner, filter to valid auctions where:
  - `auction.governance == required governance`
  - `auction.receiver == fee burner address`
  - `auction.want == configured want_address`
- Require exactly one valid match per fee burner for v1.
- Store that auction's validated `want_address` and `auction_version` on the fee burner record.
- If zero or multiple valid matches are found, mark auction refresh failed for that fee burner and skip live kicking for it.
- This intentionally fails closed if config drifts or if multiple matching auctions appear.

## Transaction Service Plan

### Candidate model

Generalize `KickCandidate` to source-neutral fields:

- `source_type`
- `source_address`
- `source_name`
- `context_type`
- `context_address`
- `context_name`
- `token_address`
- `auction_address`
- `want_address`
- `normalized_balance`
- `price_usd`
- `usd_value`
- `decimals`
- `token_symbol`
- `want_symbol`

### Candidate selection

Shortlist from a union of:

- strategy balances joined through `strategies`
- fee burner balances joined through `fee_burners`

Cooldown should key on:

- `source_address`
- `token_address`

not strategy-only fields.

### Kicker prepare/send path

- Live balance read remains `ERC20.balanceOf(sourceAddress)`.
- Quote path remains identical.
- `wantToken` should be auto-supplied from persisted source metadata (`strategies.want_address` or `fee_burners.want_address`), not manually entered at execution time.
- ABI encoding for the contract now passes:
  - `source`
  - `auction`
  - `sellToken`
  - `sellAmount`
  - `wantToken`
  - `startingPrice`
  - `minimumPrice`
- Confirmation output and logs should display `Source`, not `Strategy`.

## External Read-Only API Plan

This API lives outside this repo and must be updated in lockstep.

### Dashboard endpoint

Return combined rows with generic fields:

- `sourceType`
- `sourceAddress`
- `sourceName`
- `contextType`
- `contextAddress`
- `contextName`
- `contextSymbol`
- `auctionAddress`
- `auctionVersion`
- `wantAddress`
- `wantSymbol`
- `depositLimit` only for strategy/vault rows
- `balances`
- `kicks`
- `scannedAt`

Recommended summary additions:

- `strategyCount`
- `feeBurnerCount`
- `sourceCount`
- `tokenCount`
- `latestScanAt`

### Kick log endpoint

Return generic fields:

- `sourceType`
- `sourceAddress`
- `sourceName`
- `auctionAddress`
- `tokenAddress`
- `tokenSymbol`
- `wantAddress`
- `wantSymbol`
- `status`
- `txHash`
- `createdAt`
- `usdValue`
- `runId`

### Backward-compatibility window

For one deploy window, the API should also continue emitting legacy strategy fields on strategy rows:

- `strategyAddress`
- `strategyName`
- `vaultAddress`
- `vaultName`
- `vaultSymbol`

This keeps UI rollout safer if the API and frontend do not deploy simultaneously.

## UI Plan

### Navigation

Add a third tab:

- `Strategies`
- `Fee Burner`
- `Kick Log`

Recommended routes:

- `/strategies`
- `/fee-burner`
- `/kicklog`

### Strategies tab

- Keep current table structure.
- Filter data to `sourceType === "strategy"`.
- Continue using the shared entity cell pattern for vault and strategy identities.

### Fee Burner tab

Recommended layout:

- top summary/identity card for the burner
- source address
- auction address + version
- want token
- token balance list/table below

This is better than forcing a one-row strategy table because there is no vault context and the burner is effectively a singleton operational surface.

### Kick log

Make it source-neutral:

- column header `Strategy` -> `Source`
- detail panel label `Strategy` -> `Source`
- search should include `sourceAddress` and `sourceName`

### Shared UI rules

- Reuse the current animated copy affordance everywhere.
- Reuse `EntityIdentity` or a sibling component for fee burner identity.
- Preserve current theme behavior and contrast rules.
- Run `npm run build` and visually validate light/dark after the tab and page additions.

## File-by-File Worklist

### Contracts

- `contracts/src/AuctionKicker.sol`
  - Generalize `strategy` to `source`
  - add `wantToken` to params
  - update validation and event fields

- `contracts/test/AuctionKicker.t.sol`
  - add real fee burner fork test
  - add mixed strategy + fee burner batch test
  - update existing tuple/event expectations for the new ABI

- `contracts/README.md`
  - remove strategy-only wording
  - document generic source support and redeploy requirements

### Chain ABIs and readers

- `factory_dashboard/chain/contracts/abis.py`
  - add `FEE_BURNER_ABI`
  - update `AUCTION_KICKER_ABI` for `wantToken`

- `factory_dashboard/chain/contracts/fee_burner.py` (new)
  - add a small reader for `getApprovals(address)` and optional spender validation
  - reuse the shared trade-handler/governance address constant rather than hardcoding the spender again

- `factory_dashboard/chain/contracts/erc20.py`
  - update comments from strategy-centric holder language to source-neutral holder language

### Config and constants

- `factory_dashboard/config.py`
  - add monitored fee burner configuration
  - recommended field: `monitored_fee_burners`
  - recommended format: list of objects with required `address`, required `want_address`, and optional `label`
  - use a default empty list so existing configs remain valid

- `config.yaml`
  - register the current fee burner address, expected want address, and optional label
  - example:
    - `monitored_fee_burners:`
    - `  - address: 0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8`
    - `    want_address: 0xf939e0a03fb07f59a73314e73794be0e57ac1b4e`
    - `    label: Yearn Fee Burner`
  - keep YAML declarative only; do not add `auction_address`, token lists, or spender configuration here
  - update `auction_kicker_address` after redeploy

- `factory_dashboard/constants.py`
  - keep only true chain-wide constants here
  - reuse `YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS` for the fee burner reader's `tradeHandler` spender address
  - if config-driven registration is deferred, add a temporary fee burner registry here

### Persistence

- `factory_dashboard/persistence/models.py`
  - add fee burner tables
  - add generic source columns to `kick_txs`
  - optionally add generic source columns to `scan_item_errors`

- `factory_dashboard/persistence/repositories.py`
  - add `FeeBurnerRepository`
  - add `FeeBurnerTokenRepository`
  - add `FeeBurnerTokenBalanceRepository`
  - update `KickTxRepository` to read/write `source_*`
  - change `KickTxRepository.last_kick_for_pair(...)` to key by `source_address`, not `strategy_address`
  - keep strategy repositories intact

- `alembic/versions/<new_revision>.py`
  - create the new fee burner tables
  - add `kick_txs.source_type`
  - add `kick_txs.source_address`
  - add the new source columns as nullable first
  - backfill existing kick rows from `strategy_address`
  - tighten the new columns only after backfill succeeds
  - add new indexes for `(source_address, token_address, created_at desc)`

### Scanner

- `factory_dashboard/scanner/service.py`
  - keep existing strategy flow
  - add fee burner scan stage
  - include fee burner tokens in metadata/price refresh and balance reads
  - persist burner auction/want metadata
  - keep `scan_runs` aggregate counters source-neutral rather than adding burner-only columns in v1

- `factory_dashboard/scanner/auction_mapper.py`
  - extract reusable auction metadata lookup
  - add fee burner lookup path keyed by configured `(want, receiver)`
  - keep existing strategy mapping path unchanged

- `factory_dashboard/scanner/fee_burner.py` (new)
  - resolve approved tokens for configured burners
  - mark token origin as `trade_handler_approval`
  - produce fee burner scan rows and balance pairs

- `factory_dashboard/runtime.py`
  - wire the new reader/repositories/services into scanner construction

### Transaction service

- `factory_dashboard/transaction_service/types.py`
  - generalize `KickCandidate`
  - generalize confirmation-facing metadata

- `factory_dashboard/transaction_service/evaluator.py`
  - union strategy + fee burner shortlist queries
  - cooldown keyed by `source_address`

- `factory_dashboard/transaction_service/kicker.py`
  - encode new contract tuple with `wantToken`
  - use `source_*` fields in logs and kick rows
  - update inserted DB fields

- `factory_dashboard/transaction_service/service.py`
  - rename strategy-centric logs to source-centric logs
  - write dry-run rows with `source_*`

- `factory_dashboard/cli.py`
  - confirmation output should show `Source`
  - single-kick and batch displays should not assume strategies

### UI

- `ui/src/App.jsx`
  - add `fee-burner` route parsing
  - add the third tab
  - split rows by `sourceType`
  - keep strategies page strategy-only
  - add `FeeBurnerPage`
  - change kick log labels/search from strategy-specific to source-specific
  - key strategies-page expansion state by `sourceAddress`, not `strategyAddress`
  - leave `KickLogPage` row expansion keyed by `kick.id`
  - keep the implementation inline in `App.jsx` unless a clear reuse boundary appears; avoid file churn for this change

- `ui/src/styles.css`
  - add fee burner page/card/table styles
  - adjust tab spacing for the third tab
  - preserve current copy icon and contrast behavior in both themes

- `ui/README.md`
  - document the new page structure and payload expectations

### Docs

- `README.md`
  - update repo overview to mention fee burner scanning and source-neutral kicking
  - update strategy auction mapping section to mention fee burner `(want, receiver)` mapping
  - note the external API response expansion

### Tests

- `tests/unit/test_strategy_auction_mapper.py`
  - add fee burner auction mapping coverage for governance + configured want + receiver matching

- `tests/integration/test_scanner_service.py`
  - add configured fee burner fixture
  - assert burner rows, burner balances, burner auction metadata

- `tests/unit/test_txn_evaluator.py`
  - add fee burner shortlist cases
  - assert cooldown and union behavior

- `tests/unit/test_txn_kicker.py`
  - update for `wantToken`
  - add fee burner source candidate coverage

- `tests/integration/test_txn_service.py`
  - add mixed strategy + fee burner dry-run/live orchestration coverage

- `tests/integration/test_cli.py`
  - update confirmation text expectations from strategy to source wording

## Rollout Order

### Phase 1: Schema and backend support

1. Add migrations and repositories.
2. Add fee burner scan path.
3. Add generic source fields to kick rows.
4. Run scanner and verify fee burner rows populate correctly.

### Phase 2: External API

1. Update dashboard and kick-log queries to emit new generic fields.
2. Keep legacy strategy fields temporarily for compatibility.

### Phase 3: Contract

1. Update `AuctionKicker.sol`.
2. Run Foundry fork tests.
3. Deploy new contract.
4. Allowlist the new mech in `TradeHandler` via whatever address currently controls `tradeHandler.governance()`; if production authority is a multisig or timelock, route this through the normal governance operation.
5. Update `auction_kicker_address` in config.

### Phase 4: Transaction service

1. Update shortlist, prepare, execute, and CLI paths.
2. Run dry-run txn service first.
3. Verify one fee burner candidate end to end before enabling live batches.

### Phase 5: UI

1. Ship `Fee Burner` tab.
2. Keep `Strategies` page unchanged for real strategy rows.
3. Make `Kick Log` source-neutral.
4. Run build and visual checks in light and dark themes.

### Phase 6: Cleanup

- Once API and UI are fully cut over, decide whether to:
  - stop emitting legacy `strategy*` fields from the external API
  - stop relying on `kick_txs.strategy_address`
  - add a later migration to remove obsolete columns

## Verification Checklist

### Contract

- `forge test` passes on a mainnet fork.
- mixed strategy + fee burner batch passes.
- wrong `wantToken` reverts with `want mismatch`.
- wrong `source` reverts with `receiver mismatch`.

### Scanner

- fee burner row exists after a scan.
- fee burner auction and want token are populated.
- approved burner tokens appear in persistence.
- zero balances behave as expected.

### Transaction service

- dry-run includes fee burner candidates.
- live prepare reads burner balances correctly.
- cooldown works on fee burner rows.
- submitted/confirmed kick rows write `source_type` and `source_address`.

### UI

- `Strategies` tab still looks and behaves the same for strategy rows.
- `Fee Burner` tab renders without any fake vault language.
- `Kick Log` displays both strategies and fee burner entries under `Source`.
- `npm run build` passes in `ui/`.
- light/dark themes and copy affordances are validated manually.

## Non-Goals For This Change

- Full schema rename from `strategies` to `sources`
- Rewriting the strategy discovery pipeline to be generic
- Supporting multiple burner auctions per receiver in the first release

## Recommendation Summary

Implement this as:

- generic contract + txn source model
- additive fee burner persistence
- external API union layer
- dedicated UI tab for the fee burner

This keeps the strategy dashboard clean, avoids fake strategy/vault rows, and still gives a sound path for future non-strategy sell sources.
