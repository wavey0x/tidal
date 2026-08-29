# Server Operator Guide

Use this page after [Install](install.md). It focuses on the supported `tidal-server` command surface, not on host-specific scheduler or init-system setup.

## What The Server Operator Owns

`tidal-server` is the server operator CLI. It owns:

- Alembic migrations
- scan execution
- FastAPI serving
- API key management
- the canonical SQLite database

## Supported Commands

The intended runtime surface is:

- `tidal-server db migrate`
- `tidal-server db repair-auction-rounds`
- `tidal-server db clear-no-fill-suspension`
- `tidal-server scan run`
- `tidal-server api serve`
- `tidal-server auth ...`

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

Set `RPC_URL` for scanner reconciliation and on-chain reads. Receipt finalization
uses the same reconciler for local execution, API-reported receipts, and scans.
Before alert evaluation, scans also retry canonical repair for current ambiguous
auction rounds. Successful repairs do not generate an operational notification.

## Scan Execution

Default scan:

```bash
tidal-server scan run --config config/server.yaml
```

Scan with auto-settle enabled for that invocation:

```bash
tidal-server scan run --config config/server.yaml --auto-settle --no-confirmation
```

Scan with token auto-enable enabled for that invocation:

```bash
tidal-server scan run --config config/server.yaml --auto-enable-tokens --no-confirmation
```

When either transaction automation flag is used, the server also needs valid local wallet configuration such as:

- `TXN_KEYSTORE_PATH`
- `TXN_KEYSTORE_PASSPHRASE`

## Operator Actions From The Server Host

Use `tidal`, not `tidal-server`, even when you are working directly on the server machine:

```bash
export TIDAL_API_BASE_URL=http://127.0.0.1:8787
tidal kick inspect
tidal kick run
tidal logs kicks
```

If API auth is enabled, create a client key with `tidal-server auth create` and export `TIDAL_API_KEY` before using `tidal`.

## Config Notes

Common server settings include:

- `auction_factory_address`
- `auction_kicker_address`
- `monitored_fee_burners`
- `kick`

The API bind defaults live in code: `tidal_api_host=0.0.0.0` and `tidal_api_port=8787`.
Set them explicitly only when you need a non-default bind.

Most scanner, pricing, multicall, and reconcile tuning also now defaults in code. Only override those via environment variables when you are intentionally tuning a deployment.

For Telegram operational alerts, configure all three neutral `TELEGRAM_*`
variables documented in [Configuration](config.md) in the ignored server `.env`.

Scan auto-settle and token auto-enable are not configured in `server.yaml`.
Enable them explicitly with `--auto-settle` or `--auto-enable-tokens` when needed.

For scan-side transaction automation, keep `TXN_KEYSTORE_PATH` and `TXN_KEYSTORE_PASSPHRASE` in the service environment. Use `--keystore` and `--password-file` only for one-off overrides when running `tidal`.

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

When upgrading an installation with retained auction history, stop all writers,
back up the database, apply migrations, and follow the full repair procedure in
[Server Database Commands](cli-server-db.md). Do not restart automation until a
second repair pass reports zero mutations.

Runtime behavior:

- journal mode: WAL
- busy timeout: 30 seconds
- synchronous mode: NORMAL

That configuration is set in `tidal/persistence/db.py`.
