# Curve Gauge `is_killed()` Kick Guard

## Goal

Add one extra live safety check before a strategy kick is prepared.

Today automated kicks can be blocked with manual `kick.ignore` rules in
`config/server.yaml`. This plan adds an automated guard: if a strategy farms a
Curve gauge and that gauge's `is_killed()` call returns `true`, Tidal should not
prepare or trigger a kick for that strategy by default.

Manual CLI runs can bypass the guard with an explicit flag.

## Rules

- Apply the guard only to `strategy` candidates.
- Do not apply it to fee burners.
- Check the gauge during prepare/planning, before kick calldata is built.
- Block only when the gauge read succeeds and returns `true`.
- Continue normally when the gauge read returns `false`.
- Continue normally when the strategy has no readable gauge, the gauge lacks
  `is_killed()`, or the read fails.
- Keep the existing `kick.ignore` list. This is an extra guard, not a replacement.

The core policy is:

```text
is_killed() == true blocks; everything else does not block
```

## CLI Override

Add one `tidal kick run` flag:

```bash
tidal kick run --allow-killed-gauge
```

Default behavior is no override.

When this flag is present:

- the CLI sends `allowKilledGauge: true` in each prepare request
- the API passes it into the planner
- the planner skips the killed-gauge guard for that request
- the prepared action audit payload records `allowKilledGauge`

Do not add a server config setting for the override. Automation should get the
safe default unless an operator intentionally passes the CLI flag.

## Implementation Steps

1. Add a minimal Curve gauge ABI.

   File: `tidal/chain/contracts/abis.py`

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

2. Add a small strategy gauge reader.

   File: `tidal/chain/contracts/yearn.py`

   Keep it narrow:

   - resolve the gauge address from the strategy using the deployed strategy's
     actual accessor
   - call `is_killed()` on that gauge
   - return `True`, `False`, or `None`

   Use `None` for unsupported or failed reads. The planner should treat `None`
   as "do not block".

   Before coding, confirm the strategy accessor name. If the current deployed
   strategies consistently use `gauge()`, support only `gauge()`. Add a second
   accessor only if the deployed set proves it is needed.

3. Wire the reader into `KickPlanner`.

   File: `tidal/transaction_service/planner.py`

   Add an optional dependency such as `strategy_gauge_reader=None` to the planner
   constructor, then in `plan(...)` add an `allow_killed_gauge: bool = False`
   argument.

   After shortlist selection and before auction settlement inspection:

   - collect strategy candidates
   - if `allow_killed_gauge` is false, check those strategies
   - move candidates with `is_killed() == True` into
     `plan.skipped_during_prepare`
   - use reason `strategy gauge is killed`
   - prepare the remaining candidates normally

   Do not move this into scanner caching or the initial SQL shortlist.

4. Thread the override through API prepare.

   Files:

   - `tidal/api/schemas/kick.py`
   - `tidal/api/routes/kick.py`
   - `tidal/api/services/action_prepare.py`

   Add request field:

   ```python
   allow_killed_gauge: bool = Field(default=False, alias="allowKilledGauge")
   ```

   Pass it to `prepare_kick_action(...)`, then to `KickPlanner.plan(...)`.
   Include `allowKilledGauge` in the prepared action request payload.

5. Thread the override through the CLI.

   File: `tidal/kick_cli.py`

   Add:

   ```text
   --allow-killed-gauge
   ```

   For default runs, omit `allowKilledGauge` or send `false`. When the flag is
   present, send `allowKilledGauge: true` in prepare payloads.

6. Keep output simple.

   Reuse existing prepare skip rendering and headless skip events. The operator
   should see:

   ```text
   strategy gauge is killed
   ```

   Do not redesign skip payloads for this feature.

## Tests

Add focused tests only:

- planner skips a strategy candidate when the reader returns `True`
- planner allows a strategy candidate when the reader returns `False`
- planner allows a strategy candidate when the reader returns `None`
- `allow_killed_gauge=True` bypasses the reader result
- fee-burner candidates are not checked
- API prepare accepts and forwards `allowKilledGauge`
- prepared action audit payload includes `allowKilledGauge`
- CLI `--allow-killed-gauge` sends `allowKilledGauge: true`
- default CLI run does not set the override

Reader tests can use mocked Web3 calls. No fork test is required for the first
implementation.

## Documentation After Implementation

Update:

- `docs/cli-client-kick.md`
- `docs/kick-selection.md`
- `AUTOMATE.md`

Document that automation should omit `--allow-killed-gauge`.

## Non-Goals

- No database migration.
- No scanner cache for killed gauge state.
- No background polling.
- No server config toggle for the override.
- No broad ABI probing framework.
- No inspect UI changes in the first pass unless implementation work proves it is
  trivial.

## Acceptance Checklist

- A killed strategy gauge blocks kick preparation by default.
- A non-killed, unreadable, or unsupported gauge does not block.
- Fee-burner kicks are unchanged.
- `tidal kick run --allow-killed-gauge` bypasses only this guard.
- API action audit records whether the override was used.
- Existing manual `kick.ignore` behavior is unchanged.
- Focused planner, API, and CLI tests pass.
