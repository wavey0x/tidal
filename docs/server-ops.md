# Server Operator Guide

Use this page after [Install](install.md). It focuses on the supported `tidal-server` command surface, not on host-specific scheduler or init-system setup.

## What The Server Operator Owns

`tidal-server` is the server operator CLI. It owns:

- Alembic migrations
- scan execution
- optional server-local kick execution
- FastAPI serving
- API key management
- the canonical SQLite database

## Supported Commands

The intended runtime surface is:

- `tidal-server db migrate`
- `tidal-server scan run`
- `tidal-server api serve`
- `tidal-server kick run`

Repeated invocation and scheduling are external concerns.

## First-Time Bootstrap

After following [Install](install.md), review:

- `config/server.yaml`
- `~/.tidal/server/.env`, or `TIDAL_ENV_FILE` for an explicit path
- `TIDAL_HOME` if you want mutable files outside `~/.tidal`

Then run:

```bash
tidal-server db migrate --config config/server.yaml
tidal-server auth create --label cli-client-name --config config/server.yaml
tidal-server scan run --config config/server.yaml
tidal-server api serve --config config/server.yaml
```

If the API should reconcile receipts in the background, set `RPC_URL` so the API process can start its reconciler.

## Scan Execution

Default scan:

```bash
tidal-server scan run --config config/server.yaml
```

Scan with auto-settle enabled for that invocation:

```bash
tidal-server scan run --config config/server.yaml --auto-settle --no-confirmation
```

When `--auto-settle` is used, the server also needs valid local wallet configuration such as:

- `TXN_KEYSTORE_PATH`
- `TXN_KEYSTORE_PASSPHRASE`

## Kick Execution

Inspect:

```bash
tidal-server kick inspect --config config/server.yaml
```

Run once with explicit wallet selection:

```bash
tidal-server kick run --config config/server.yaml --sender 0xYourAddress --account wavey3
```

Run once without an interactive confirmation step:

```bash
tidal-server kick run --config config/server.yaml --no-confirmation --sender 0xYourAddress --account wavey3
```

## Config Notes

Common server settings include:

- `scan_concurrency`
- `monitored_fee_burners`
- `kick`

The API bind defaults live in code: `tidal_api_host=0.0.0.0` and `tidal_api_port=8787`.
Set them explicitly only when you need a non-default bind.

Scan auto-settle is not configured in `server.yaml`.
Enable it explicitly with `--auto-settle` when needed.

## API Key Management

Create:

```bash
tidal-server auth create --label alice --config config/server.yaml
```

List:

```bash
tidal-server auth list --config config/server.yaml
```

Revoke:

```bash
tidal-server auth revoke alice --config config/server.yaml
```

The API stores only SHA-256 hashes of keys. The plaintext key is shown once at creation time.

## Database Notes

SQLite is the canonical datastore for this repo.

Runtime behavior:

- journal mode: WAL
- busy timeout: 30 seconds
- synchronous mode: NORMAL

That configuration is set in `tidal/persistence/db.py`.
