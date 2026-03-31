# Server Operator: `tidal-server scan`

`tidal-server scan` is the discovery and state-refresh surface for the server operator.

## Subcommands

- `run`: execute one scan cycle
- `daemon`: run repeated scan cycles on an interval

## Common Invocations

Run one scan immediately:

```bash
tidal-server scan run --config config/server.yaml
```

Run the scanner continuously:

```bash
tidal-server scan daemon --config config/server.yaml --interval-seconds 300
```

Emit machine-readable output:

```bash
tidal-server scan run --config config/server.yaml --json
```

## Required Inputs

At minimum, scanner execution needs:

- `RPC_URL` in `config/.env` or `TIDAL_ENV_FILE`
- monitored fee burners in `config/server.yaml`

The scanner populates the shared SQLite cache that powers:

- the dashboard
- kick inspection
- kick preparation
- log history

## Important Config

Common server operator settings for this command:

- `scan_interval_seconds`
- `monitored_strategy_factories`
- `monitored_fee_burners`
- `scan_auto_settle_enabled`

## Auto-Settle Note

If `scan_auto_settle_enabled` is enabled, the server also needs valid local wallet configuration such as:

- `TXN_KEYSTORE_PATH`
- `TXN_KEYSTORE_PASSPHRASE`

Without that, the scan will fail when it reaches the settlement path.
