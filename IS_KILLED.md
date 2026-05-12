# Curve Gauge `is_killed()` Kick Guard Plan

## Goal

Add a live safety check before automated strategy kick transactions are prepared.

If a strategy's Curve gauge can be read and `is_killed()` returns `true`, Tidal should skip kicking that strategy by default. Manual CLI runs can override this with an explicit flag.

This complements the existing manual `kick.ignore` rules in `config/server.yaml`; it does not replace them.

## Default Behavior

- Strategy candidates get a best-effort live gauge check before transaction preparation.
- If the gauge check returns `true`, the candidate is skipped and no kick transaction is prepared.
- If the gauge returns `false`, preparation continues normally.
- Fee-burner candidates are not checked because they are not strategy gauge farms.
- If the strategy gauge cannot be resolved, or the gauge does not support `is_killed()`, do not block the candidate.
- If the check is skipped or unsupported, keep the reason visible enough for debugging but do not turn it into a hard failure.

Keep the hard rule simple:

```text
only a successful is_killed() == true blocks a kick
```

## Manual Override

Add one CLI flag to `tidal kick run`:

```bash
tidal kick run --allow-killed-gauge
```

Behavior:

- Default is `false`.
- Headless automation should not pass this flag.
- When the flag is present, killed-gauge skips are bypassed for that run.
- Include the override in the prepare request as `allowKilledGauge`.
- Store `allowKilledGauge` in the prepared action request payload so manual overrides are audit-visible.

Do not add a server config setting for this in the first implementation. A durable config switch would make it easier to accidentally disable the guard for automation.

## Where The Check Belongs

Put the enforcement in the prepare/planning path, not only in inspect.

Primary path:

- `tidal/kick_cli.py`
  - add `--allow-killed-gauge`
  - pass `allowKilledGauge` in each prepare payload
- `tidal/api/schemas/kick.py`
  - add `allow_killed_gauge: bool = Field(default=False, alias="allowKilledGauge")`
- `tidal/api/routes/kick.py`
  - thread the value into `prepare_kick_action`
- `tidal/api/services/action_prepare.py`
  - thread the value into `KickPlanner.plan`
  - include it in `create_prepared_action(... request_payload=...)`
- `tidal/transaction_service/planner.py`
  - check selected strategy candidates before preparing kick intents
  - add killed candidates to `plan.skipped_during_prepare`
  - continue preparing other candidates when possible

Optional but useful after the prepare guard works:

- `tidal/ops/kick_inspect.py`
  - show killed strategies as a non-ready state, e.g. `killed_gauge`

Prepare-time enforcement is the safety boundary. Inspect output is operator visibility.

## Gauge Reader

Add the smallest chain reader needed.

Suggested files:

- `tidal/chain/contracts/abis.py`
  - add a minimal gauge ABI:

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

- `tidal/chain/contracts/yearn.py`
  - add a small strategy gauge reader near the other strategy readers
  - resolve the strategy's gauge address using the actual accessor exposed by these Yearn Curve strategies
  - read `is_killed()` from the gauge

Implementation detail to confirm before coding:

- Identify the strategy gauge accessor used by the current deployed strategy contracts. If it is consistently `gauge()`, implement only that. If the deployed set uses two common names, support those two explicitly. Do not add a broad generic probing framework.

Return a small result object or tuple:

```python
GaugeKilledStatus(
    strategy_address: str,
    gauge_address: str | None,
    is_killed: bool | None,
    status: str,  # "ok", "no_gauge", "unsupported", "error"
    error_message: str | None = None,
)
```

The planner only blocks when:

```python
status == "ok" and is_killed is True
```

## Planner Flow

In `KickPlanner.plan(...)`:

1. Build the shortlist as today.
2. Sort selected candidates as today.
3. If `allow_killed_gauge` is false, run killed-gauge checks for strategy candidates.
4. Move killed candidates into `plan.skipped_during_prepare` with reason `strategy gauge is killed`.
5. Keep non-strategy candidates and non-killed strategy candidates in the normal preparation flow.
6. Leave auction settlement checks, live balance reads, pricing, gas estimation, and batching unchanged.

This keeps the new guard as a narrow pre-prepare filter.

## Operator Output

For candidate-level skips, reuse the existing skip rendering path.

Suggested skip detail:

```text
strategy gauge is killed
```

If a gauge address is available, include it in the skip payload as extra detail only if it fits the existing payload shape cleanly. Do not redesign skip payloads just for this.

Headless logs should emit the same candidate skip event style already used by `tidal kick run --headless`.

## Tests

Add focused tests only.

- `tests/unit/test_kick_planner.py`
  - killed strategy candidate is skipped and no kick transaction is prepared
  - `allow_killed_gauge=True` bypasses the skip
  - gauge `false` allows normal preparation
  - fee-burner candidates are not checked
  - unsupported/missing `is_killed()` does not block
- `tests/unit/test_action_prepare.py`
  - `prepare_kick_action` threads `allow_killed_gauge`
  - prepared action audit payload includes `allowKilledGauge`
- `tests/integration/test_api_control_plane.py`
  - `/kick/prepare` accepts `allowKilledGauge`
- `tests/unit/test_kick_cli.py`
  - `tidal kick run --allow-killed-gauge` passes `allowKilledGauge: true`
  - default run does not bypass the guard

If the gauge reader is added as a separate class, test it with mocked Web3 calls instead of fork tests.

## Documentation Updates During Implementation

Update these once the behavior exists:

- `docs/cli-client-kick.md`
  - document `--allow-killed-gauge`
- `docs/kick-selection.md`
  - add killed gauge to prepare-time skip reasons
- `AUTOMATE.md`
  - note that automation should omit `--allow-killed-gauge`

## Non-Goals

- No database migration.
- No cached killed-gauge state in scanner tables.
- No background polling service.
- No server config toggle for the guard.
- No attempt to infer killed status from token prices, auction state, or manual ignore rules.
- No broad ABI discovery system.

## Rollout Checklist

1. Confirm the live strategy gauge accessor name.
2. Add minimal ABI and reader.
3. Add planner-level guard.
4. Thread `allowKilledGauge` from CLI to API to planner.
5. Add focused tests.
6. Update operator docs.
7. Run the Python test subset covering planner, API prepare, and CLI.
