# Runtime Simplification Plan

## Goal

Simplify the server runtime model so the supported production shape is:

1. `tidal-server api serve`
2. `tidal-server scan run` from a systemd timer
3. `tidal-server kick run --no-confirmation` from a systemd timer

The scanner may still perform auto-settle, but only when that behavior is explicitly enabled for the invocation.

## Decisions

- Add an explicit scan auto-settle CLI override.
- CLI override takes precedence over config.
- Any effective scan auto-settle path requires `--no-confirmation`.
- Remove `scan daemon`.
- Remove `kick daemon`.
- Defer `kick run --batch` for now.

## Desired Post-Change Semantics

### `tidal-server scan run`

- Read-only by default.
- Supports `--auto-settle` to enable settlement for that run.
- Supports `--no-auto-settle` to force read-only mode even if config enables auto-settle.
- If effective auto-settle is enabled, the command must require `--no-confirmation`.
- If effective auto-settle is enabled, transaction execution prerequisites must be present.

### `tidal-server kick run`

- Remains the one-shot kick execution command.
- `--no-confirmation` remains required for unattended usage.
- No `--batch` addition in this change.

### Removed Commands

- `tidal-server scan daemon`
- `tidal-server kick daemon`

## Implementation Workstreams

### 1. Add explicit scan auto-settle override

Update the scan CLI so the operator can control settlement behavior directly from the command invocation.

Required behavior:

- Introduce a tri-state CLI option:
  - `--auto-settle`
  - `--no-auto-settle`
  - default: unset
- Compute an `effective_auto_settle` value:
  - CLI override when provided
  - otherwise fall back to `scan_auto_settle_enabled` from config
- Use `effective_auto_settle` for:
  - unattended confirmation policy
  - keystore/passphrase requirements
  - scanner construction/runtime behavior

Implementation note:

- Avoid mutating loaded settings in place unless there is a clear existing pattern for it.
- Prefer threading the effective value through the scan CLI/runtime boundary explicitly.

Acceptance criteria:

- `tidal-server scan run` stays read-only by default.
- `tidal-server scan run --auto-settle` fails unless `--no-confirmation` is also present.
- `tidal-server scan run --auto-settle --no-confirmation` enables settlement.
- `tidal-server scan run --no-auto-settle` stays read-only even if config enables auto-settle.

### 2. Keep config support, but make CLI authoritative per run

This change does not remove `scan_auto_settle_enabled` yet.

Planned behavior:

- Config remains the baseline/default.
- CLI remains the authoritative per-run override.
- Documentation should present the CLI flag as the preferred operational control for timer units.

Reasoning:

- Sending transactions should be explicit in the systemd unit or shell history.
- Operators should not need to inspect YAML to know whether a scheduled scan may broadcast transactions.

### 3. Remove daemon mode

Remove internal loop-based daemon commands from the supported CLI surface.

Code removal scope:

- delete `scan daemon`
- delete `kick daemon`
- delete now-unused interval CLI options if they are only used by daemon commands
- remove config fields that only exist for daemon scheduling

Config cleanup target:

- remove `scan_interval_seconds`

Documentation cleanup target:

- stop describing daemons as operational commands
- standardize on oneshot services plus systemd timers

Acceptance criteria:

- `tidal-server scan --help` no longer lists `daemon`
- `tidal-server kick --help` no longer lists `daemon`
- config templates no longer expose daemon-only interval settings

### 4. Update docs and operator guidance

Rewrite the runtime docs around the simplified model.

Docs to update:

- `RUN.md`
- `README.md`
- `docs/cli-server-scan.md`
- `docs/cli-server-kick.md`
- `docs/server-ops.md`
- `docs/operator-guide.md`
- `docs/config.md`
- any analysis/design docs that still describe daemon mode as active behavior

Docs should show:

- `api serve` as the only long-running process
- `scan run` on a timer
- `kick run --no-confirmation` on a timer
- `scan run --auto-settle --no-confirmation` as the explicit unattended settle form
- CLI-over-config precedence for auto-settle
- daemon removal as an intentional simplification

### 5. Verification

Minimum verification:

1. CLI help output reflects the new surface area.
2. Scan validation enforces `--no-confirmation` whenever effective auto-settle is enabled.
3. Scan validation does not require `--no-confirmation` for explicitly read-only runs.
4. Auto-settle runs still require transaction credentials.
5. Docs and examples no longer reference daemon commands as supported operator paths.

Suggested tests:

- CLI-level validation tests for:
  - `scan run --auto-settle`
  - `scan run --auto-settle --no-confirmation`
  - `scan run --no-auto-settle` with config enabled
- regression coverage around scan runtime construction with effective auto-settle override

## Explicit Non-Goals

- Do not add `kick run --batch` in this change.
- Do not redesign kick batching semantics in this pass.
- Do not merge kick execution into scan.
- Do not introduce a new long-running worker model.

## Breaking Change Notes

This is an intentional CLI simplification and should be treated as a breaking operator-facing change.

Operators will need to:

- replace any `scan daemon` usage with a timer invoking `scan run`
- replace any `kick daemon` usage with a timer invoking `kick run --no-confirmation`
- move scan auto-settle intent into the command line where appropriate

## Suggested Execution Order

1. Implement scan auto-settle CLI override and effective-value wiring.
2. Remove daemon commands and daemon-only config/option plumbing.
3. Update docs and examples to the timer-only operational model.
4. Run CLI validation tests and targeted command help checks.
