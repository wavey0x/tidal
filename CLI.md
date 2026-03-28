# CLI Implementation Plan

## Product Decision

This plan assumes no backward-compatibility requirement.

- Do not preserve `tidal txn`.
- Do not preserve standalone deploy helper UX.
- Do not keep legacy command names just to avoid churn.
- Optimize for operator clarity, implementation quality, and automation.

The end state should be one coherent CLI for the five core operator actions:

- scan
- deploy auctions
- enable tokens
- kick auctions
- view logs

## Success Criteria

The work is done when all of the following are true:

1. An operator can complete the five core actions entirely from the CLI without falling back to the UI or one-off scripts.
2. All mutating commands share the same safety model:
   - preview by default
   - `--broadcast` for writes
   - `--bypass-confirmation` to skip confirmation
   - explicit sender and wallet selection
3. All important command results are available in both human-readable text and stable JSON.
4. The CLI answers "why did this happen?" well enough that operators do not need to open SQLite manually.
5. The command tree uses operator verbs, not internal implementation names.

## Final Command Surface

This should be the supported CLI surface after the refactor.

```text
tidal scan run [--config FILE] [--json]
tidal scan daemon [--config FILE] [--interval-seconds N] [--json]

tidal auction deploy --want TOKEN --receiver ADDRESS [--factory ADDRESS]
                     [--governance ADDRESS] [--starting-price INT] [--salt HEX]
                     [--broadcast] [--bypass-confirmation]
                     [--sender ADDRESS]
                     [--account NAME | --keystore FILE]
                     [--password-file FILE] [--json]

tidal auction enable-tokens AUCTION [--extra-token TOKEN ...]
                            [--broadcast] [--bypass-confirmation]
                            [--sender ADDRESS]
                            [--account NAME | --keystore FILE]
                            [--password-file FILE] [--json]

tidal auction settle AUCTION [--token TOKEN]
                            [--method auto|settle|sweep-and-settle]
                            [--broadcast] [--bypass-confirmation]
                            [--sender ADDRESS]
                            [--account NAME | --keystore FILE]
                            [--password-file FILE] [--json]

tidal kick run [--source ADDRESS] [--auction ADDRESS] [--limit N]
               [--broadcast] [--bypass-confirmation]
               [--sender ADDRESS]
               [--account NAME | --keystore FILE]
               [--password-file FILE] [--json] [--explain]

tidal kick daemon [--source ADDRESS] [--auction ADDRESS] [--interval-seconds N]
                  [--broadcast]
                  [--sender ADDRESS]
                  [--account NAME | --keystore FILE]
                  [--password-file FILE] [--json]

tidal kick inspect [--source ADDRESS] [--auction ADDRESS] [--limit N]
                   [--show-all] [--json]

tidal logs kicks [--source ADDRESS] [--auction ADDRESS] [--status STATUS]
                 [--limit N] [--json]

tidal logs scans [--status STATUS] [--limit N] [--json]

tidal logs show RUN_ID [--json]
```

Deliberate removals:

- remove `tidal txn`
- remove bare `tidal scan` in favor of `tidal scan run`
- stop treating `tidal/auction_migration/deploy_single_auction.py` as a supported operator entrypoint once `auction deploy` lands

## Command Behavior Contract

Every command should follow the same operating rules.

### Output

- Default output is human-readable text.
- `--json` returns a stable envelope:

```json
{
  "command": "kick.run",
  "status": "ok",
  "warnings": [],
  "data": {}
}
```

- `status` values:
  - `ok`
  - `noop`
  - `error`

### Mutating command safety

- Default mode is preview, never a broadcast write.
- `--broadcast` enables broadcast/write behavior.
- If `--broadcast` is set and `--bypass-confirmation` is not set, prompt once for confirmation.
- Only prompt for:
  - confirmation
  - keystore password when not provided by `--password-file`
- Do not prompt for optional workflow choices that can be expressed as flags.

### Sender and wallet selection

- Mirror Foundry's operator model: `--sender ADDRESS` chooses the address, wallet flags choose how it is signed.
- `--sender ADDRESS` replaces `--caller ADDRESS` in the public CLI contract.
- In preview mode, `--sender` becomes the `from` address for `eth_call`-based previews.
- In broadcast mode, sender resolution order should be:
  1. Explicit `--sender ADDRESS`
  2. Exactly one configured wallet backend
  3. Otherwise fail with a validation error
- Do not copy Foundry's default fallback sender. For a work tool, silent fallback hides operator mistakes.
- Support Foundry-style wallet flags:
  - `--account NAME` loads `~/.foundry/keystores/NAME`
  - `--keystore FILE` loads an explicit keystore path
  - `--password-file FILE` supplies the keystore password non-interactively
- Environment-based password loading can remain as a fallback, but it should not be the primary documented interface.
- Keep the wallet option layout extensible for future additions such as hardware wallets or remote signers.
- Do not add `--unlocked` unless there is a concrete need to send via RPC-managed accounts.

### Exit codes

Use deterministic exit codes for automation:

- `0`: success
- `2`: no eligible work / noop
- `3`: validation or configuration error
- `4`: execution failure
- `5`: partial failure

## Design Decisions

### 1. Keep the transaction engine, refactor the operator surface

Do not rewrite:

- `tidal/transaction_service/service.py`
- `tidal/transaction_service/evaluator.py`
- `tidal/transaction_service/kicker.py`

Those modules are already a workable core. The refactor should improve:

- command naming
- explainability
- service boundaries
- renderer reuse
- logs access

### 2. Keep `tidal/cli.py` as the shell

`pyproject.toml` points at `tidal.cli:app`, so the practical move is:

- keep `tidal/cli.py` as the Typer root
- move command bodies and shared helpers into sibling modules

Do not create a `tidal/cli/` package in the first pass. It adds avoidable entrypoint churn.

### 3. Put orchestration in services, not in Typer command bodies

CLI modules should:

- parse arguments
- build context
- call application services
- render results

They should not own:

- DB query logic
- signer discovery logic
- deploy orchestration
- kick explainability assembly
- logs query logic

### 4. Treat logs as a first-class read-side

The persistence layer already contains useful audit data. The CLI should expose it directly instead of routing operators to the UI.

## Target File Layout

Keep the package layout simple and compatible with the current entrypoint:

```text
tidal/
  cli.py
  cli_context.py
  cli_exit_codes.py
  cli_options.py
  cli_renderers.py
  scan_cli.py
  kick_cli.py
  logs_cli.py
  auction_cli.py

  ops/
    auction_enable.py
    deploy.py
    kick_inspect.py
    logs.py
```

File responsibilities:

- `tidal/cli.py`
  - build the root Typer app
  - register `scan`, `auction`, `kick`, and `logs` sub-apps
  - no large command bodies

- `tidal/cli_context.py`
  - shared CLI context object
  - settings loading
  - DB session factory access
  - sync Web3 construction
  - signer resolution hooks

- `tidal/cli_options.py`
  - reusable Typer option declarations
  - consistent option names/help strings

- `tidal/cli_exit_codes.py`
  - named constants for command exit behavior

- `tidal/cli_renderers.py`
  - text rendering
  - JSON envelope emission
  - shared tables and summaries

- `tidal/scan_cli.py`
  - `scan run`
  - `scan daemon`

- `tidal/kick_cli.py`
  - `kick run`
  - `kick daemon`
  - `kick inspect`

- `tidal/logs_cli.py`
  - `logs kicks`
  - `logs scans`
  - `logs show`

- `tidal/auction_cli.py`
  - `auction deploy`
  - `auction enable-tokens`
  - `auction settle`
  - no large business-logic blocks

- `tidal/ops/deploy.py`
  - extracted deploy orchestration from `auction_migration/deploy_single_auction.py`

- `tidal/ops/kick_inspect.py`
  - explainability layer for kick candidate decisions

- `tidal/ops/logs.py`
  - read-side query API over `txn_runs`, `kick_txs`, `scan_runs`, and `scan_item_errors`

## Work Packages

## Phase 1: CLI Foundation

### Goal

Create the shared plumbing before adding new command surface.

### Files to add

- `tidal/cli_context.py`
- `tidal/cli_exit_codes.py`
- `tidal/cli_options.py`
- `tidal/cli_renderers.py`
- `tidal/scan_cli.py`
- `tidal/kick_cli.py`
- `tidal/logs_cli.py`

### Files to change

- `tidal/cli.py`
- `tidal/auction_cli.py`
- `tests/unit/test_cli.py`
- `tests/integration/test_cli.py`

### Tasks

1. Reduce `tidal/cli.py` to a root app that only wires subcommands together.
2. Introduce a `CLIContext` object with helpers for:
   - loading settings
   - opening DB sessions
   - constructing sync Web3
   - resolving signers
3. Move repeated Typer option definitions into `cli_options.py`.
4. Move shared output helpers into `cli_renderers.py`.
5. Introduce standard JSON envelope rendering and standard exit-code usage.

### Done when

- new command modules are registered from `tidal/cli.py`
- no major business logic remains in the root CLI module
- text and JSON output can be reused by multiple commands

## Phase 2: Logs First

### Goal

Make logs a first-class operator workflow before further CLI renaming.

### Files to add

- `tidal/ops/logs.py`

### Files to change

- `tidal/logs_cli.py`
- `tidal/persistence/repositories.py`
- `tests/unit/test_cli.py`
- `tests/integration/test_cli.py`

### Tasks

1. Add query functions for:
   - recent kick attempts
   - recent txn runs
   - recent scan runs
   - recent scan item errors
   - run detail by `run_id`
2. Expose `tidal logs kicks` with filters:
   - `--source`
   - `--auction`
   - `--status`
   - `--limit`
3. Expose `tidal logs scans` with recent scan runs and recent scan item failures.
4. Expose `tidal logs show RUN_ID` to print:
   - run metadata
   - candidate counts
   - per-attempt status
   - failure reason
   - quote URL when present
5. Ensure JSON output includes enough detail for automation and incident review.

### Done when

- an operator can answer "what failed last?" from the CLI
- quote URL and failure reason are visible from logs
- the UI is no longer required for routine history inspection

## Phase 3: Kick Command Refactor

### Goal

Replace `txn` with a proper `kick` surface and expose explainability.

### Files to change

- `tidal/kick_cli.py`
- `tidal/transaction_service/service.py`
- `tidal/transaction_service/evaluator.py`
- `tidal/transaction_service/kicker.py`
- `tidal/transaction_service/types.py`
- `tidal/runtime.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_txn_evaluator.py`
- `tests/unit/test_txn_kicker.py`
- `tests/integration/test_cli.py`
- `tests/integration/test_txn_service.py`

### Files to add

- `tidal/ops/kick_inspect.py`

### Tasks

1. Remove `txn` from the supported CLI surface.
2. Add:
   - `tidal kick run`
   - `tidal kick daemon`
   - `tidal kick inspect`
3. Standardize result objects from the transaction service so commands can render:
   - eligible
   - deferred
   - cooldown skipped
   - same-auction skipped
   - quote failure
   - preflight failure
   - execution failure
4. Add `kick inspect` to show why a candidate is or is not kickable without sending anything.
5. Add `--explain` to `kick run` so preview and broadcast output includes richer decision detail.
6. Ensure failure output includes:
   - clear reason text
   - quote status
   - full quote URL when present

### Done when

- `kick` is the only operator-facing command name for this workflow
- `kick inspect` explains "No eligible candidates" clearly
- one-shot and daemon modes share the same rendering and filtering model

## Phase 4: Deploy as a First-Class CLI Workflow

### Goal

Port single-auction deploy into the main CLI and remove the standalone helper from the supported workflow.

### Files to add

- `tidal/ops/deploy.py`

### Files to change

- `tidal/auction_cli.py`
- `tidal/cli_support.py`
- `tidal/auction_migration/deploy_single_auction.py`
- `tests/unit/test_cli.py`
- `tests/integration/test_cli.py`

### Tasks

1. Extract deploy orchestration from `tidal/auction_migration/deploy_single_auction.py`.
2. Add `tidal auction deploy` with flags for:
   - `--factory`
   - `--want`
   - `--receiver`
   - `--governance`
   - `--starting-price`
   - `--salt`
   - `--broadcast`
   - `--bypass-confirmation`
   - `--sender`
   - `--account`
   - `--keystore`
   - `--password-file`
3. Keep the strong preview behavior from the current helper:
   - show existing matches
   - show predicted auction address
   - show deployment payload
4. Reuse the shared sender/wallet handling from the CLI foundation work.
5. Once parity is reached, retire `deploy_single_auction.py` as a supported entrypoint.

### Done when

- deployment is performed through `tidal auction deploy`
- deploy output matches the quality of enable/kick flows
- there is no separate deploy CLI dialect

## Phase 5: Enable-Tokens and Sweep Consistency

### Goal

Make the remaining auction mutation commands look and behave like the rest of the CLI.

### Files to change

- `tidal/auction_cli.py`
- `tidal/ops/auction_enable.py`
- `tidal/cli_support.py`
- `tests/unit/test_auction_enable.py`
- `tests/unit/test_cli.py`
- `tests/integration/test_cli.py`

### Tasks

1. Move remaining enable-token orchestration out of the CLI function body and into `tidal/ops/auction_enable.py`.
2. Make `enable-tokens` use the same result model shape as deploy and kick:
   - preview result
   - execution result
   - warnings
   - per-token detail
3. Add semantic `auction settle` orchestration around the same flags:
   - `--broadcast`
   - `--bypass-confirmation`
   - `--token`
   - `--method`
   - `--sender`
   - `--account`
   - `--keystore`
   - `--password-file`
   - `--json`
4. Make `auction settle` pick the correct action automatically:
   - no active lot -> noop
   - sold-out active lot -> `settle()`
   - active lot at or below floor -> `sweepAndSettle()`
   - active lot above floor -> noop
5. Standardize live-send output for every mutating command:
   - sender
   - tx hash
   - broadcast timestamp
   - receipt status when available
   - block and gas-used when available
6. Keep prompts minimal:
   - confirmation only
   - password only
7. Ensure both commands have machine-readable output that does not require text scraping.

### Done when

- deploy, enable, settle, and kick all use one safety and output model
- automation can call any mutation command in JSON mode

## Phase 6: Docs, Cleanup, and Removal of Legacy Surface

### Goal

Make the new CLI the only documented operator surface.

### Files to change

- `README.md`
- `FEATURES.md`
- `tests/integration/test_cli.py`

### Files to remove after parity

- `tidal/auction_migration/deploy_single_auction.py`

### Tasks

1. Rewrite docs around operator tasks, not internal modules.
2. Remove docs/examples that mention:
   - `tidal txn`
   - bare `tidal scan`
   - standalone deploy helper usage
3. Update `--help` text and examples for every final command.
4. Confirm the integration test suite reflects the final command surface only.

### Done when

- the README teaches only the new command tree
- internal implementation names are not leaked into operator docs

## Testing Plan

Testing should be done alongside each phase, not saved for the end.

### Unit coverage

- `tests/unit/test_cli.py`
  - root command registration
  - help text
  - option parsing
  - JSON envelope rendering

- `tests/unit/test_txn_evaluator.py`
  - explainability reason codes
  - cooldown and same-auction decision detail

- `tests/unit/test_txn_kicker.py`
  - failure reason propagation
  - quote URL propagation

- `tests/unit/test_auction_enable.py`
  - result shaping for preview/broadcast

Add new unit coverage as needed:

- deploy service tests
- logs query tests
- renderer tests if logic becomes non-trivial

### Integration coverage

- `tests/integration/test_cli.py`
  - `scan run`
  - `kick run`
  - `kick inspect`
  - `logs kicks`
  - `logs show`
  - `auction deploy`
  - `auction enable-tokens`

- `tests/integration/test_txn_service.py`
  - keep core transaction-service behavior coverage

### Manual verification

At the end of the refactor, manually verify:

1. `tidal kick inspect` explains an ineligible source clearly.
2. `tidal logs show <run_id>` displays full quote URL and full failure reason.
3. `tidal auction deploy` works in preview mode with only flags and no unnecessary prompts.
4. `tidal auction enable-tokens` and `tidal auction settle` follow the same broadcast/confirmation/signer pattern.

## Execution Order

Implement in this order:

1. Phase 1: CLI foundation
2. Phase 2: logs
3. Phase 3: kick
4. Phase 4: deploy
5. Phase 5: enable/sweep consistency
6. Phase 6: docs and cleanup

This order is deliberate:

- shared plumbing first avoids duplicate refactors
- logs first improves operator visibility immediately
- kick next fixes the most important transaction workflow
- deploy after that removes the largest remaining UX outlier

## Non-Goals

These are not part of this plan unless a later pass explicitly adds them:

- rewriting the transaction engine from scratch
- introducing a separate TUI
- adding backward-compatibility aliases
- preserving legacy script entrypoints for sentimental reasons
