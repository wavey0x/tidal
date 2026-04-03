# Kick Backend Simplification Plan

## Goal

Remove the remaining transition-state complexity from the kick backend.

The specific target is to stop using `AuctionKicker` as a runtime dependency and make the system run directly on:

- `KickPreparer`
- `KickTxBuilder`
- `KickExecutor`
- `KickPlanner`
- `TxnService`

This is a cleanup and clarification project, not a redesign.
Breaking changes are acceptable.
There is no requirement to preserve the old facade or any backward-compatible entry points.

## Primary Outcome

After this work:

- `runtime.py` will construct the split components directly.
- `TxnService` will orchestrate only the split components.
- `KickPlanner` will require the split components directly.
- `action_prepare.py` will use the split planner boundary directly.
- `kicker.py` will be deleted.

## Why This Is The Highest-Leverage Next Step

The codebase has already paid the cost of splitting preparation, tx building, and execution.
The main remaining complexity is the compatibility layer and the dual-mode orchestration around it.

Today the code still carries these unnecessary burdens:

- `TxnService` supports both the split path and the legacy wrapper path.
- `KickPlanner` still has compatibility fallback behavior.
- `runtime.py` still constructs the old wrapper even though the real logic lives elsewhere.
- tests still rely on `kicker.py` patch points that no longer represent the intended architecture.

That is accidental complexity. Removing it is a simplification, not an expansion.

## Design Principles

1. Prefer deletion over abstraction.
2. Keep the system runnable after every phase.
3. Do not change API payloads or DB schema in this project.
4. Do not refactor unrelated domains in the same branch.
5. Prefer direct breakage plus test migration over preserving legacy paths.
6. Avoid creating new helper classes unless they immediately remove more complexity than they add.

## Non-Goals

These are out of scope:

- UI refactors
- scanner refactors
- persistence schema changes
- generic planner/executor framework
- action plugin system
- deploy/enable/settle redesign
- generalized DI container

## Current Complexity To Remove

### 1. Runtime Dual Wiring

`runtime.py` still constructs `AuctionKicker`, then reaches through it to get the real components.

That means the old public surface still appears to be the system entry point even though it is no longer the real domain boundary.

### 2. Service Dual-Mode Behavior

`TxnService` still accepts `kicker`, optional `preparer`, optional `executor`, and planner/non-planner modes.

Some of this is real orchestration complexity.
Some is only there to support the old wrapper and old tests.

### 3. Planner Compatibility Paths

`KickPlanner` still contains compatibility behavior for callers that do not provide the direct split dependencies.

That weakens the boundary and makes the type contract less clear.

### 4. Facade Debt

`kicker.py` is now mostly an object-shaped mirror of three concrete components.

That file is not the domain anymore.
It is transition-state glue.

## Target Architecture

### `KickPreparer`

Responsibility:

- inspect candidates
- read live balances
- apply sizing and pricing policy
- perform quote-based preparation
- produce `PreparedKick`, `PreparedSweepAndSettle`, or `KickResult`

Must not:

- sign transactions
- send transactions
- persist `kick_txs`

### `KickTxBuilder`

Responsibility:

- build calldata and `TxIntent`s from prepared operations

Must not:

- inspect auctions
- read balances
- call quote APIs
- persist anything

### `KickExecutor`

Responsibility:

- persist execution-phase failures
- estimate execution transactions
- send transactions
- wait for receipts
- persist submitted/confirmed/reverted outcomes

Must not:

- call quote APIs
- recompute pricing decisions
- rebuild preparation logic

### `KickPlanner`

Responsibility:

- shortlist
- inspect once
- prepare candidates
- build intents
- estimate plan-time executability
- perform batch fallback logic
- return a `KickPlan`

Must not:

- know about `AuctionKicker`
- write directly to DB
- broadcast transactions

### `TxnService`

Responsibility:

- manage run lifecycle
- request a plan when using planner mode
- execute prepared operations
- count attempts / successes / failures
- finalize `txn_runs`

Must not:

- contain compatibility fallback logic for the old facade
- know how to build calldata
- know how to prepare kicks itself

## Implementation Strategy

This should be done in small phases.
Each phase should leave the repo green.
Each phase should be committed independently.
No phase should preserve legacy behavior just to ease migration.

## Phase 0: Lock In Behavior Before More Deletion

### Objective

Make sure the current split behavior is explicitly covered before deleting the remaining legacy boundary.

### Work

Add or confirm tests for:

- planner live path counting prepare-time `ERROR` as failure
- planner live path counting prepare-time `SKIP` as not attempted
- planner live path persisting prepare-time failures through executor behavior
- non-planner live path parity for prepare-time failures
- action-prepare using the direct planner boundary

### Exit Criteria

- the transaction-service semantics are explicit in tests
- there is no remaining ambiguity about attempt/failure counting

## Phase 1: Make `KickExecutor` The Only Public Failure Recorder

### Objective

Remove `TxnService` dependence on executor private internals.

### Work

In `kick_execute.py`:

- add a public method like `record_prepare_failure(...)`
- make it accept:
  - `run_id`
  - `candidate`
  - `result`
- have it persist the failure row and return a normalized `KickResult`

Use the same audit fields currently preserved by `_persist_prepare_failure`.

### Why First

This reduces risk in later phases.
It gives `TxnService` a stable public contract before legacy code is deleted.

### Verification

- unit tests for `record_prepare_failure(...)`
- existing txn-service tests still pass

### Commit Boundary

One commit dedicated to publicizing prepare-failure persistence.

## Phase 2: Simplify `TxnService` Around Direct Dependencies

### Objective

Make `TxnService` depend only on the real split boundary.

### Work

In `service.py`:

- require `preparer` and `executor` in the constructor for production usage
- replace:
  - any `self.preparer or ...` pattern
  - any `self.executor or ...` pattern
  - any signer fallback through `kicker`
- use only:
  - `self.preparer`
  - `self.executor`
  - `self.planner`

### Additional Cleanup

Delete dead legacy state from `TxnService`, including any unused `tx_builder` placeholder if it is not needed.

### Verification

- `tests/integration/test_txn_service.py`
- focused planner tests
- full suite if phase is small enough

### Commit Boundary

One commit for `TxnService` simplification only.

## Phase 3: Make `KickPlanner` Strict

### Objective

Turn `KickPlanner` into a single clear boundary with no legacy object-shape fallbacks.

### Work

In `planner.py`:

- remove `kicker` constructor argument
- require:
  - `preparer`
  - `tx_builder`
  - either `web3_client` or explicit estimate function
- delete object-shape compatibility checks entirely
- delete calldata fallback behavior that exists only for fake kicker-style tests

### Test Migration

Update planner tests to provide explicit fake:

- preparer
- tx builder
- web3 client or estimate function

Direct-contract fakes are acceptable in tests.
The rule is that production code must not support kicker-shaped fallback behavior at all.

### Verification

- `tests/unit/test_kick_planner.py`
- `tests/unit/test_action_prepare.py`

### Commit Boundary

One commit for planner strictness and test migration.

## Phase 4: Rewire `runtime.py` To Construct Real Components Directly

### Objective

Make runtime reflect the real architecture.

### Work

In `runtime.py`:

1. construct `KickPreparer`
2. construct `KickTxBuilder`
3. construct `KickExecutor`
4. construct `KickPlanner`
5. construct `TxnService`

Do not construct `AuctionKicker`.

Pass the explicit components directly into `TxnService` and `KickPlanner`.

### Important Constraint

Do not change operational configuration behavior in this phase.
Only change object construction and wiring.

### Verification

- `tests/integration/test_api_control_plane.py`
- `tests/integration/test_txn_service.py`
- command-adjacent tests that use `build_txn_service`

### Commit Boundary

One commit for runtime rewiring only.

## Phase 5: Update `action_prepare.py` To Use The Direct Boundary Only

### Objective

Remove remaining API-side reconstruction of planner dependencies from possible `kicker` state.

### Work

In `action_prepare.py`:

- stop looking for `txn_service.kicker`
- use:
  - `txn_service.planner` if already built
  - otherwise construct `KickPlanner` from direct components only

If tests currently monkeypatch `build_txn_service` to return `SimpleNamespace(kicker=...)`:

- update those tests immediately to return `planner`, `preparer`, and `tx_builder`, or a fake `TxnService`-shaped object with the direct dependencies

### Verification

- `tests/unit/test_action_prepare.py`
- `tests/integration/test_api_control_plane.py`

### Commit Boundary

One commit for API-prepare migration.

## Phase 6: Migrate Tests Off `kicker.py`

### Objective

Stop test architecture from preserving production legacy debt.

### Work

Replace patches against:

- `tidal.transaction_service.kicker.ERC20Reader`
- `tidal.transaction_service.kicker.logger`
- `AuctionKicker`

with direct tests for:

- `KickPreparer`
- `KickTxBuilder`
- `KickExecutor`

### Recommended Split

Create or expand:

- `tests/unit/test_kick_prepare.py`
- `tests/unit/test_kick_tx.py`
- `tests/unit/test_kick_execute.py`

Move legacy facade-oriented cases into the concrete component suites.

### Verification

- full transaction-service unit suite
- full repo suite before deleting the facade

### Commit Boundary

This may need two commits:

- one for new direct tests
- one for removing old facade-oriented tests

## Phase 7: Delete `AuctionKicker` Runtime Behavior

### Objective

Remove the legacy facade from production code.

### Work

Delete `kicker.py` completely and update imports.

### Verification

- no production module imports `AuctionKicker`
- no runtime path constructs `AuctionKicker`
- `kicker.py` no longer exists
- full suite green

### Commit Boundary

Final cleanup commit removing the facade.

## Optional Phase 8: Shrink `KickExecutor` Further

### Objective

Only after the facade is gone, decide whether `KickExecutor` is still too mixed.

### Work

If it is still carrying too much persistence noise:

- extract a very small helper focused only on `kick_txs` row assembly and failure recording

Do this only if it produces a visibly simpler `KickExecutor`.

Do not do this reflexively.

### Success Test

If `KickExecutor` is understandable after the facade is removed, skip this phase.

## File-By-File Edit Checklist

### `tidal/transaction_service/kick_execute.py`

- add public failure-recording API
- keep execution logic intact
- avoid introducing new abstractions unless necessary

### `tidal/transaction_service/service.py`

- remove runtime fallback branches
- use explicit dependencies only
- keep run counting and finalization semantics unchanged

### `tidal/transaction_service/planner.py`

- remove `kicker` compatibility arguments and object-shape fallbacks
- require explicit dependencies

### `tidal/runtime.py`

- instantiate split components directly
- stop constructing `AuctionKicker`

### `tidal/api/services/action_prepare.py`

- depend on direct planner inputs only

### `tidal/transaction_service/kicker.py`

- delete

### Tests

- migrate old facade tests into concrete component suites
- keep behavior coverage, not interface nostalgia

## Risk Management

### Main Risk 1: Silent Change In Failure Accounting

Mitigation:

- preserve explicit tests for prepare-time `SKIP`, `ERROR`, and `ESTIMATE_FAILED`
- verify `kicks_attempted`, `kicks_failed`, and persisted rows together

### Main Risk 2: API Prepare Regressions

Mitigation:

- keep serialized preview and transaction payloads unchanged
- verify with existing API control-plane tests

### Main Risk 3: Test Migration Hides Production Breakage

Mitigation:

- migrate production wiring and test wiring in the same phase where a legacy dependency is removed
- run full suite after each major phase

### Main Risk 4: Over-Refactoring

Mitigation:

- no recorder extraction until after the facade is removed
- no new generic framework
- no unrelated domain cleanup in this branch

## Verification Plan

Run after each phase as appropriate:

- `python -m compileall tidal tests`
- `pytest tests/unit/test_kick_planner.py -q`
- `pytest tests/integration/test_txn_service.py -q`
- `pytest tests/unit/test_action_prepare.py -q`
- `pytest tests/integration/test_api_control_plane.py -q`

Before final merge:

- `pytest -q`

## Stop Conditions

Stop and reassess if any of these happen:

- a phase requires new generic abstractions to proceed
- API payload compatibility becomes unclear
- test migration starts tempting the addition of new legacy shims
- the branch stops being cleanly incremental

If that happens, cut the phase smaller.

## Done Means

This work is complete when all of the following are true:

- `runtime.py` does not construct `AuctionKicker`
- `TxnService` does not branch on legacy wrapper availability
- `KickPlanner` does not accept or use `kicker`
- `action_prepare.py` does not look for `txn_service.kicker`
- tests do not patch `tidal.transaction_service.kicker.ERC20Reader`
- `kicker.py` is gone
- full suite passes

## Recommended Commit Sequence

1. Public `KickExecutor` failure-recording API
2. `TxnService` direct-dependency cleanup
3. `KickPlanner` strict dependency cleanup
4. `runtime.py` direct construction
5. `action_prepare.py` direct boundary cleanup
6. test migration away from `kicker.py`
7. delete `kicker.py`
8. optional executor shrink pass if still justified

## Final Note

The success condition is not “more abstraction.”
The success condition is that a new reader can follow the kick path in one straight line:

- shortlist
- prepare
- build intents
- execute
- persist

If a step does not make that line straighter, it should not be part of this project.
