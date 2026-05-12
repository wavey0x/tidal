# Curve Gauge `is_killed()` Kick Guard

## Goal

Add one extra safety check for strategy kicks and make the result visible in the
dashboard.

Today automated kicks can be blocked with manual `kick.ignore` rules in
`config/server.yaml`. This plan adds an automated guard: during regular scanner
runs, Tidal records whether a strategy's Curve gauge reports `true` from
`is_killed()`. If it does, kick preparation should skip that strategy by default.

The UI should show a small warning in the strategy table so operators can see
that a strategy/auction is disabled before they try to kick it.

Manual CLI runs can bypass this guard with an explicit flag.

## Rules

- Discover killed gauges during normal scanner runs.
- Persist the latest kick guard status in SQLite.
- Apply the guard only to `strategy` candidates.
- Do not apply it to fee burners.
- Block only when the persisted latest status says the strategy is disabled.
- Continue normally when the status is missing, unknown, unreadable, or enabled.
- Keep the existing `kick.ignore` list. This is an extra guard, not a replacement.

The core policy is:

```text
scanner records disabled status; prepare blocks only disabled == true
```

## Database

Add one latest-state table. Do not add a history table.

Suggested table: `kick_guard_status_latest`

Keep the table generic enough to hold other future disabled reasons, but only
populate Curve gauge status in the first implementation.

Columns:

- `source_type` text, part of primary key
- `source_address` text, part of primary key
- `auction_address` text, nullable
- `disabled` integer, `0` or `1`
- `reason` text, nullable
- `detail` text, nullable
- `checked_at` text, not null
- `block_number` integer, nullable

Initial reason values:

- `curve_gauge_killed`: disabled because `is_killed()` returned `true`
- `curve_gauge_active`: gauge was read and returned `false`
- `curve_gauge_unknown`: gauge address or `is_killed()` could not be read

Only `disabled = 1` should block prepare or show the UI warning. Unknown status
is stored for debugging but should not block.

Files to update:

- `tidal/persistence/models.py`
- `tidal/persistence/repositories.py`
- `alembic/versions/...`
- `tidal/_resources/alembic/versions/...`

Keep the repository API small:

```python
upsert_many(rows)
get_many(source_type, source_addresses)
```

## Scanner Discovery

Add a small strategy gauge reader.

Files:

- `tidal/chain/contracts/abis.py`
- `tidal/chain/contracts/yearn.py`
- `tidal/runtime.py`
- `tidal/scanner/service.py`

Add the minimal gauge ABI:

```python
CURVE_GAUGE_ABI = [
    {
        "inputs": [],
        "name": "is_killed",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    }
]
```

The reader should:

- resolve the gauge address from the strategy using the deployed strategy's
  actual accessor
- call `is_killed()` on that gauge
- return `True`, `False`, or `None`

Use `None` for unsupported or failed reads.

Before coding, confirm the strategy accessor name. If the deployed strategies
consistently use `gauge()`, support only `gauge()`. Add a second accessor only if
the deployed set proves it is needed.

In `ScannerService.scan_once(...)`, add one scanner stage after strategy auction
mapping and before balance reads:

1. collect mapped strategy rows
2. read gauge killed status for those strategies
3. upsert `kick_guard_status_latest`
4. record failures as `curve_gauge_unknown`, not scan failures

This keeps the discovery in the regular scan path without adding a new worker.

## Prepare Guard

File: `tidal/transaction_service/planner.py`

Add `allow_killed_gauge: bool = False` to `KickPlanner.plan(...)`.

After shortlist selection and before auction settlement inspection:

1. load persisted guard status for selected strategy candidates
2. if `allow_killed_gauge` is false, skip candidates with `disabled = 1`
3. add skipped candidates to `plan.skipped_during_prepare`
4. use reason `strategy gauge is killed`
5. prepare the remaining candidates normally

Do not re-read every gauge during prepare in the first implementation. The
regular scanner already owns discovery, and kick selection already depends on
fresh scanner data.

## CLI Override

Add one `tidal kick run` flag:

```bash
tidal kick run --allow-killed-gauge
```

Default behavior is no override.

When this flag is present:

- the CLI sends `allowKilledGauge: true` in each prepare request
- the API passes it into the planner
- the planner skips this guard for that request
- the prepared action audit payload records `allowKilledGauge`

Do not add a server config setting for the override. Automation should get the
safe default unless an operator intentionally passes the CLI flag.

Files:

- `tidal/kick_cli.py`
- `tidal/api/schemas/kick.py`
- `tidal/api/routes/kick.py`
- `tidal/api/services/action_prepare.py`

Request field:

```python
allow_killed_gauge: bool = Field(default=False, alias="allowKilledGauge")
```

## Dashboard Warning

Expose the persisted status through the dashboard read model.

Files:

- `tidal/read/dashboard.py`
- `tidal/api/services/dashboard.py` only if service glue is needed
- `ui/src/App.jsx`
- `ui/src/styles.css`

In `DashboardReadService`, left join the latest guard status for strategy rows
and add fields similar to:

```json
{
  "kickGuardDisabled": true,
  "kickGuardReason": "curve_gauge_killed",
  "kickGuardDetail": "Curve gauge is killed",
  "kickGuardCheckedAt": "..."
}
```

In the strategy table Last Scan cell, keep the relative time as-is. If
`kickGuardDisabled` is true, render a yellow warning emoji on the next line under
the relative time:

```jsx
<span className="scan-warning" title={row.kickGuardDetail}>⚠️</span>
```

Use the native `title` tooltip for the first pass. Keep the cell compact and do
not add explanatory text in the table.

The warning should appear only for disabled statuses, not for unknown gauge
reads.

## Tests

Add focused tests only:

- scanner upserts `disabled = 1` with `curve_gauge_killed` when the reader
  returns `True`
- scanner upserts `disabled = 0` when the reader returns `False`
- scanner upserts `disabled = 0` with `curve_gauge_unknown` when the reader
  returns `None`
- planner skips a strategy candidate when persisted status has `disabled = 1`
- planner allows a strategy candidate when status is missing or `disabled = 0`
- `allow_killed_gauge=True` bypasses the persisted disabled status
- fee-burner candidates are not checked or blocked
- API prepare accepts and forwards `allowKilledGauge`
- prepared action audit payload includes `allowKilledGauge`
- CLI `--allow-killed-gauge` sends `allowKilledGauge: true`
- dashboard API includes guard status fields
- UI renders the warning emoji in the Last Scan column when
  `kickGuardDisabled` is true

Reader tests can use mocked Web3 calls. No fork test is required for the first
implementation.

For UI changes, run `npm run build` in `ui/` and visually check light and dark
themes when the implementation is done.

## Documentation After Implementation

Update:

- `docs/cli-client-kick.md`
- `docs/kick-selection.md`
- `AUTOMATE.md`

Document that automation should omit `--allow-killed-gauge`.

## Non-Goals

- No guard history table.
- No background polling.
- No server config toggle for the override.
- No broad ABI probing framework.
- No custom tooltip component for the first UI pass.
- No fee-burner disabled-state work in the first implementation.

## Acceptance Checklist

- Regular scanner runs persist latest guard status per strategy.
- A killed strategy gauge produces a persisted disabled status.
- Kick preparation blocks persisted disabled strategy candidates by default.
- Non-killed, unreadable, unsupported, or missing status does not block.
- Fee-burner kicks are unchanged.
- `tidal kick run --allow-killed-gauge` bypasses only this guard.
- API action audit records whether the override was used.
- Dashboard rows include persisted disabled status.
- The strategy table Last Scan cell shows a yellow ⚠️ with hover tooltip under
  the relative time when disabled.
- Existing manual `kick.ignore` behavior is unchanged.
- Focused scanner, planner, API, CLI, dashboard, and UI tests pass.
