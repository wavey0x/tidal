# API Refactor Plan

## Product Decision

This plan assumes the new Tidal operator model is:

- the server owns the canonical DB
- the server runs scans and other shared background jobs
- operator CLIs talk to the server over HTTP
- operator wallets stay local
- the server prepares actions and records audit history
- the CLI signs and broadcasts transactions

This also assumes there is no requirement to preserve the current Tidal API shape in `wavey-api`.

## Core Recommendation

Do not let multiple operator CLIs talk to the shared SQLite database directly.

That design creates the wrong coupling:

- schema changes become client-breaking
- auth and audit are weak
- coordination between operators is poor
- every operator machine needs DB and RPC assumptions
- write workflows become race-prone

Use an API-backed control plane instead.

That gives the right split:

- server owns shared state, logs, audit history, and read models
- CLI keeps private keys and signs locally
- UI and CLI share the same server-side business logic
- operators do not need direct DB access

## Success Criteria

The refactor is done when all of the following are true:

1. `tidal` is an operator-only CLI that exposes only API-backed commands.
2. `tidal-server` is a superset CLI that exposes the operator commands plus `db migrate`, `scan run`, `scan daemon`, `kick daemon`, and `api serve`.
3. `tidal` does not expose `scan` or `db` commands at all.
4. `logs`, `kick inspect`, `kick run`, `auction deploy`, `auction enable-tokens`, and `auction settle` can be driven from a remote operator CLI through the API.
5. The API prepare flow is advisory, not a locking mechanism, and the system accepts rare duplicate or stale prepares as an acceptable tradeoff for lower complexity.
6. All Tidal API code lives in this monorepo.
7. `wavey-api` no longer contains Tidal-specific SQL, routes, or tests.
8. The UI consumes the monorepo API, not the old `wavey-api` Tidal endpoints.
9. Every prepared or broadcasted write has an audit trail that includes:
   - operator identity
   - action id
   - sender
   - tx hash
   - broadcast timestamp
   - receipt status, block, gas used when available

## Final Architecture

### 1. Server

The server process owns:

- SQLite
- Alembic migrations
- scanner daemons
- optional automated kick daemon
- HTTP API
- action audit trail

The server should run close to the DB and RPC.

### 2. Operator CLI

The operator CLI should:

- call the API for reads and action preparation
- keep wallet selection local
- sign locally
- broadcast locally
- report the tx hash and final receipt details back to the API

The CLI should not open the shared DB in normal operator mode.

### 3. CLI Packaging

Use two entrypoints into the same package:

- `tidal`
  - operator CLI
  - installed on laptops and other operator machines
  - exposes only API-backed commands
- `tidal-server`
  - server/admin CLI
  - installed on the machine that owns the SQLite database
  - exposes everything in `tidal` plus server-only commands

Both entrypoints should be thin Typer roots over the same underlying command modules. No duplicated business logic.

### 4. UI

The UI should call the same API for:

- dashboard rows
- kick logs
- Auctionscan lookups
- deploy preparation

The UI should stop depending on the old `wavey-api` Tidal route layout.

## CLI Command Surface

### `tidal`

This is the operator CLI.

Commands:

- `tidal logs kicks`
- `tidal logs scans`
- `tidal logs show`
- `tidal kick inspect`
- `tidal kick run`
- `tidal auction deploy`
- `tidal auction enable-tokens`
- `tidal auction settle`

Properties:

- API-backed only
- no `scan`
- no `db`
- no local SQLite workflow

### `tidal-server`

This is the server/admin CLI.

It exposes everything in `tidal`, plus:

- `tidal-server db migrate`
- `tidal-server scan run`
- `tidal-server scan daemon`
- `tidal-server kick daemon`
- `tidal-server api serve`
- `tidal-server auth create --label <name>`
- `tidal-server auth list`
- `tidal-server auth revoke <label>`

Important implication:

- `tidal scan run` does not exist on operator installs at all
- there is no hidden client mode and no runtime flag that toggles help output
- if someone wants to run a scan, they use `tidal-server` on the server

## CLI Configuration

The operator CLI needs remote control-plane configuration.

Add shared API options:

- `--api-base-url`
- `--api-key`

Preferred environment variables:

- `TIDAL_API_BASE_URL`
- `TIDAL_API_KEY`

Recommended behavior:

- `tidal` uses these settings for API-backed operator commands
- `tidal-server` exposes local DB and scan commands separately; no runtime mode switching
- do not keep a documented local direct-DB operator mode in `tidal`

Settings-loading rule:

- `--config` remains available on both entrypoints
- `tidal` may load config for API settings such as `TIDAL_API_BASE_URL` and `TIDAL_API_KEY`
- loading operator config must not resolve, validate, or initialize DB state
- DB path and `database_url` must stay lazy and server-only in practice

## Control-Plane Rules

### Preparation and broadcast split

Every mutating operator workflow should become a two-phase flow:

1. CLI requests a prepared action from the API.
2. API returns:
   - action id
   - warnings
   - human/JSON preview payload
   - one or more unsigned tx requests
3. CLI confirms with the operator.
4. CLI signs and broadcasts locally.
5. CLI reports broadcast metadata to the API.
6. CLI reports final receipt metadata to the API after waiting for confirmation.

### Race model

The prepare response is advisory, not a lock.

That means:

- state can change between prepare and broadcast
- a manual operator and the daemon can still act near the same time
- very rare duplicate or stale transactions are acceptable in this design

The reason for this choice is simplicity:

- the scanner cadence is low
- manual sends are exceptional
- the cost of a rare duplicate transaction is low
- removing reservations cuts a lot of system complexity

The daemon does not need to participate in an API locking flow. It continues to use the existing server-local execution path and its existing `kick_txs` / `txn_runs` history.

### Sender policy

The API should never hold private keys.

The API may still need the intended sender for simulation and authorization checks. The sender should therefore be part of prepare requests.

Rules:

- CLI resolves sender locally from `--sender`, `--account`, `--keystore`, or `--password-file`
- prepare endpoints accept `sender`
- API uses `sender` for preview simulation and mech authorization checks
- API returns tx requests only; it does not sign them

## Final API Surface

Use a single explicit prefix:

```text
/api/v1/tidal
```

Do not keep route aliases like `/tidal`, `/factory-dashboard`, and `/api/tidal`.

Breaking changes get a new version prefix. Non-breaking changes are additive within v1.

### Health endpoint

```text
GET  /health
```

Outside the versioned prefix. Returns 200 when the server is ready to accept requests. Needed for deployment readiness checks.

### Read endpoints (public, no auth)

```text
GET  /api/v1/tidal/dashboard
GET  /api/v1/tidal/logs/kicks?limit=&offset=&status=&source=&auction=
GET  /api/v1/tidal/logs/scans?limit=&offset=&status=
GET  /api/v1/tidal/logs/runs/{run_id}
GET  /api/v1/tidal/kicks/{kick_id}/auctionscan
GET  /api/v1/tidal/strategies/{strategy}/deploy-defaults
POST /api/v1/tidal/kick/inspect
```

### Prepare endpoints (auth required)

```text
POST /api/v1/tidal/kick/prepare
POST /api/v1/tidal/auctions/deploy/prepare
POST /api/v1/tidal/auctions/{auction}/enable-tokens/prepare
POST /api/v1/tidal/auctions/{auction}/settle/prepare
```

Notes:

- `strategies/{strategy}/deploy-defaults` is the UI convenience read endpoint
- `auctions/deploy/prepare` is the generic CLI/operator endpoint
- the UI should fetch defaults first, then call the generic deploy prepare endpoint

### Action lifecycle endpoints (auth required)

```text
GET  /api/v1/tidal/actions?limit=&offset=&operator=&status=&action_type=
GET  /api/v1/tidal/actions/{action_id}
POST /api/v1/tidal/actions/{action_id}/broadcast
POST /api/v1/tidal/actions/{action_id}/receipt
```

## Response Contract

### Standard envelope

All endpoints should use one envelope:

```json
{
  "status": "ok",
  "warnings": [],
  "data": {}
}
```

Allowed `status` values:

- `ok`
- `noop`
- `error`

### Prepare response

Each prepare endpoint should return:

```json
{
  "status": "ok",
  "warnings": [],
  "data": {
    "actionId": "uuid",
    "actionType": "kick",
    "preview": {},
    "transactions": [
      {
        "operation": "kick",
        "to": "0x...",
        "data": "0x...",
        "value": "0x0",
        "chainId": 1,
        "sender": "0x...",
        "gasEstimate": 210000,
        "gasLimit": 252000
      }
    ]
  }
}
```

For `kick`, `transactions` may contain more than one item:

- zero or more `sweep-and-settle` txs first
- then one `batchKick` tx

That allows the CLI to preserve the current action ordering while still signing locally.

### Broadcast report

`POST /actions/{action_id}/broadcast` should record:

- sender
- tx hash
- broadcast timestamp
- transaction index within the action plan

### Receipt report

`POST /actions/{action_id}/receipt` should record:

- receipt status
- block number
- gas used
- gas price if known
- observed at timestamp

## Authentication and Audit

Authentication matters once multiple operators use the same control plane.

### Minimum version

Require a bearer token for all operator endpoints.

The token should resolve to an operator identity such as:

- `wavey`
- `alice`
- `bob`

API keys are stored in the database and managed through the server CLI:

```
tidal-server auth create --label wavey
tidal-server auth list
tidal-server auth revoke wavey
```

- one key per operator label (labels must be unique)
- keys are hashed (SHA-256) at rest; the raw key is shown once at creation
- revoked keys are soft-deleted (`revoked_at` timestamp)
- no OAuth, session login, or token rotation service

### CORS

The API must explicitly allow the UI origin.

Keep this simple:

- configured allowed origins
- enabled for the UI-facing read and prepare endpoints
- no wildcard production CORS

### Pagination

All list-style read endpoints should use the same pagination model:

- `limit`
- `offset`

Keep the defaults boring:

- default `limit`: 100
- max `limit`: 500

Do not introduce cursor pagination in this cut.

### Required audit fields

Every prepared action should record:

- action id
- action type
- operator id
- requested at
- sender
- resource identifiers
- preview payload snapshot

Every broadcasted action should additionally record:

- tx hash
- broadcast at
- receipt fields when available

## Database Changes

Add three new tables for authentication and control-plane lifecycle tracking.

### `api_keys`

One row per operator API key.

Fields:

- `label` (PK) — unique operator label
- `key_hash` — SHA-256 hex digest of the raw key
- `key_prefix` — first 8 characters for display
- `created_at`
- `revoked_at` — null when active, timestamp when revoked

### `api_actions`

One row per prepared operator action.

Fields:

- `action_id`
- `action_type`
- `status`
- `operator_id`
- `sender`
- `resource_address`
- `auction_address`
- `source_address`
- `token_address`
- `request_json`
- `preview_json`
- `error_message`
- `created_at`
- `updated_at`

Suggested statuses:

- `PREPARED`
- `BROADCAST_REPORTED`
- `CONFIRMED`
- `REVERTED`
- `FAILED`

`PREPARED` rows that are never broadcast are acceptable historical records. They do not need a separate expiry lifecycle.

### `api_action_transactions`

One row per unsigned tx request and later per broadcast/receipt.

Fields:

- `id`
- `action_id`
- `tx_index`
- `operation`
- `to_address`
- `data`
- `value`
- `chain_id`
- `gas_estimate`
- `gas_limit`
- `tx_hash`
- `broadcast_at`
- `receipt_status`
- `block_number`
- `gas_used`
- `gas_price_gwei`
- `error_message`
- `created_at`
- `updated_at`

Why this split:

- `kick prepare` can yield more than one transaction
- audit and reconciliation need per-tx receipt fields
- querying scalar columns is cleaner than digging through JSON blobs

Do not store per-tx lifecycle in `tx_hashes_json` or other JSON blobs.

### Receipt reconciliation

If the CLI reports a tx hash but dies before it reports the receipt, the server must reconcile it.

Add a small background sweep that polls receipts for any `api_action_transactions` row where:

- `tx_hash` is set
- `receipt_status` is null
- `broadcast_at` is older than a short threshold

This is the only background reconciliation job in scope. Do not build a more general job framework for this cut.

### SQLite note

Keep SQLite and WAL mode for this cut.

State the tradeoff explicitly:

- WAL mode is acceptable for a small number of operators and light write frequency
- if operator count or write rate grows materially, the first pressure point will be SQLite concurrency
- do not switch databases as part of this refactor

Keep `kick_txs` and `txn_runs` as the execution history generated by the server-side daemons and current local service. `api_actions` and `api_action_transactions` are the operator control-plane ledger on top.

## Monorepo Layout

Move all Tidal API code into this repo under a dedicated package.

Recommended layout:

```text
tidal/
  api/
    __init__.py
    app.py
    auth.py
    dependencies.py
    errors.py
    schemas/
      common.py
      dashboard.py
      logs.py
      actions.py
      auctions.py
      kick.py
    routes/
      dashboard.py
      logs.py
      actions.py
      auctions.py
      kick.py
    services/
      dashboard.py
      action_prepare.py
      action_audit.py
      auctionscan.py

  read/
    dashboard.py
    kick_logs.py
    scan_logs.py
    run_logs.py

  ops/
    deploy.py
    auction_enable.py
    kick_inspect.py
    ...

  control_plane/
    client.py
```

### Why this split

- `tidal/api/*` is HTTP-only glue
- `tidal/read/*` holds reusable read models for UI and logs
- `tidal/ops/*` holds business logic for mutation preparation
- `tidal/control_plane/client.py` becomes the shared HTTP client used by the CLI

## Framework Choice

Move the Tidal API to FastAPI in the monorepo.

Reasoning:

- typed request and response models matter once CLI and UI share the API
- OpenAPI docs are useful for internal operator tooling
- the repo already leans into typed Python and Pydantic-style config
- FastAPI makes it easier to keep schemas explicit than the current ad hoc Flask responses

Do not port the current Flask app structure directly into the monorepo.

## What Leaves `wavey-api`

Remove the Tidal slice from `wavey-api` completely after cutover.

Files to remove or simplify there:

- `services/tidal.py`
- Tidal route wiring from `app.py`
- `tests/test_tidal_service.py`
- Tidal-specific config keys from `config.py`

Keep unrelated `wavey-api` functionality there:

- gauge endpoints
- resupply endpoints
- other non-Tidal services

## What Gets Reused From This Repo

The monorepo API should reuse existing Tidal logic instead of continuing the current duplication.

### Reuse directly

- `tidal.ops.deploy`
- `tidal.ops.auction_enable`
- `tidal.auction_settlement`
- `tidal.ops.kick_inspect`
- `tidal.ops.logs`
- `tidal.pricing.token_price_agg`
- `tidal.persistence.*`
- `tidal.runtime`
- `tidal.config`

### Replace duplicated external logic

The current `wavey-api/services/tidal.py` duplicates too much:

- raw SQL templates for dashboard and kicks
- deploy quote parsing
- deploy price inference
- Auctionscan lookup/persistence
- schema feature detection

The in-repo API should replace that with shared monorepo services.

## One-Shot Implementation Plan

This should be implemented as one cut. Do not spend effort on backwards compatibility.

### Files to add

- `tidal/server_cli.py`
- `tidal/read/dashboard.py`
- `tidal/read/kick_logs.py`
- `tidal/read/scan_logs.py`
- `tidal/read/run_logs.py`
- `tidal/api/app.py`
- `tidal/api/auth.py`
- `tidal/api/dependencies.py`
- `tidal/api/errors.py`
- `tidal/api/routes/dashboard.py`
- `tidal/api/routes/logs.py`
- `tidal/api/routes/kick.py`
- `tidal/api/routes/auctions.py`
- `tidal/api/routes/actions.py`
- `tidal/api/schemas/*.py`
- `tidal/api/services/dashboard.py`
- `tidal/api/services/action_prepare.py`
- `tidal/api/services/action_audit.py`
- `tidal/api/services/auctionscan.py`
- `tidal/control_plane/client.py`
- `tidal/auth_cli.py`
- Alembic migration for `api_keys`
- Alembic migration for `api_actions`
- Alembic migration for `api_action_transactions`

### Files to change

- `pyproject.toml`
- `tidal/cli.py`
- `tidal/config.py`
- `tidal/ops/logs.py`
- `tidal/ops/deploy.py`
- `tidal/ops/auction_enable.py`
- `tidal/auction_settlement.py`
- `tidal/transaction_service/service.py`
- `tidal/transaction_service/kicker.py`
- `tidal/kick_cli.py`
- `tidal/auction_cli.py`
- `tidal/cli_context.py`
- `tidal/cli_options.py`
- `ui/src/App.jsx`
- `ui/vite.config.js`
- `ui/vercel.json`
- `ui/README.md`
- `/Users/wavey/yearn/wavey-api/app.py`
- `/Users/wavey/yearn/wavey-api/config.py`

### Files to remove

- `/Users/wavey/yearn/wavey-api/services/tidal.py`
- `/Users/wavey/yearn/wavey-api/tests/test_tidal_service.py`

### Work

1. Move Tidal-specific dashboard, log, and Auctionscan read logic into this repo.
2. Split the CLI package into two root entrypoints:
   - `tidal.cli:app` for the operator CLI
   - `tidal.server_cli:app` for the server/admin superset
3. Build the FastAPI app with the final `/api/v1/tidal` route surface.
4. Add DB-backed API key auth (`api_keys` table, `tidal-server auth` commands), configured CORS, and uniform `limit`/`offset` pagination.
5. Add `api_keys`, `api_actions`, and `api_action_transactions`.
6. Add receipt reconciliation for broadcasted txs that never receive a CLI receipt report.
7. Refactor `kick prepare` explicitly:
   - separate shortlist/evaluation/pricing from signing
   - return unsigned tx payloads without requiring a signer
   - preserve returned transaction order for sweep-and-settle plus batch kick
   - treat this as the main complexity in the cut; the rest should stay comparatively boring
   - add a `prepare_kick(...)` function in `tidal/ops/` that runs the same candidate evaluation and pricing as `run_once(live=False)` but returns a list of unsigned transaction dicts (`to`, `data`, `value`, `chainId`, `gasEstimate`, `gasLimit`) instead of a run summary; gas estimation uses the `sender` from the prepare request; the existing `run_once` should be refactored to call `prepare_kick` internally so there is one evaluation path, not two
8. Keep deploy, enable-tokens, and settle prepare services simple and reuse the existing in-repo ops modules.
9. Switch operator CLI commands to `tidal/control_plane/client.py`.
10. Implement the settings-loading rule from CLI Configuration so the `tidal` entrypoint never touches DB state.
11. Switch the UI to the new API prefix and route model.
12. Remove the Tidal slice from `wavey-api`.

### Done when

- operator commands only use the API
- `tidal` help output contains no DB or scan commands
- multi-tx actions have per-tx audit rows
- the server reconciles missing receipt reports
- the UI points at the monorepo API
- `wavey-api` contains no Tidal code

## Testing Plan

### Unit tests

Add new tests in this repo for:

- request/response schemas
- prepare service branching
- action audit writes
- receipt reconciliation
- dashboard read-model assembly
- Auctionscan resolution service

### Integration tests

Add integration coverage for:

- `GET /api/v1/tidal/dashboard`
- `GET /api/v1/tidal/logs/kicks`
- `GET /api/v1/tidal/actions`
- `GET /api/v1/tidal/strategies/{strategy}/deploy-defaults`
- `POST /api/v1/tidal/kick/prepare`
- `POST /api/v1/tidal/auctions/{auction}/settle/prepare`
- `POST /api/v1/tidal/actions/{action_id}/broadcast`
- `POST /api/v1/tidal/actions/{action_id}/receipt`

### CLI integration tests

Add CLI tests for:

- operator command uses API client instead of local DB
- broadcast metadata is reported to the API
- multi-tx kick prepare is executed in returned order

### Migration tests

Move the current `wavey-api/tests/test_tidal_service.py` coverage into this repo as:

- deploy-prepare tests
- dashboard read-model tests
- Auctionscan tests

## Deployment Plan

Monorepo does not need to mean a single deployable.

Recommended deployables:

1. Tidal scanner/control-plane server
   - runs scans
   - owns DB
   - serves API
   - runs receipt reconciliation
2. Tidal UI
   - static build
   - points at the control-plane API
3. Operator CLI
   - installed on operator machines
   - talks to the control-plane API

This is the clean target:

- one codebase
- three deploy surfaces
- no duplicated Tidal business logic

## Non-Goals

These are explicitly not part of this plan:

- putting private keys on the server
- letting every CLI machine open the shared SQLite file
- preserving the `wavey-api` Tidal route layout
- keeping `factory-dashboard` naming around as a first-class Tidal concept
