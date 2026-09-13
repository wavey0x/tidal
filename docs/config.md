# Configuration

Every service and SSH command loads one YAML configuration and one selected
secret file. There is no separate CLI client configuration or remote API mode.

| Setting | Selection |
|---|---|
| Policy file | `--config`, then `TIDAL_CONFIG`, then project `config/server.yaml` |
| Secret file | `TIDAL_ENV_FILE`, otherwise `~/.tidal/server/.env` |
| Mutable home | `TIDAL_HOME`, otherwise `~/.tidal` |
| Database | `DB_PATH`, otherwise `TIDAL_HOME/server/tidal.db` |
| Execution lock | `TIDAL_HOME/execution.lock` |
| Local activation | `TIDAL_HOME/activation.json`; exclude from backups |

For base settings, process environment overrides the selected secret file,
which overrides YAML and then code defaults. Explicit execution profiles apply
after base settings. One-off CLI policy flags override the selected profile.

The tracked YAML includes chain ID, AuctionKicker/factory addresses, monitored
fee burners, scanner policy and the complete `kick:` pricing/ignore policy.
Preserve that policy during deployment and recovery. The explicit profiles are:

```yaml
execution_profiles:
  scan:
    txn_usd_threshold: 250
    txn_base_fee_cap_gwei: 5
    txn_require_curve_quote: true
  strategy:
    txn_usd_threshold: 100
    txn_base_fee_cap_gwei: 1
    txn_require_curve_quote: true
  fee_burner:
    txn_usd_threshold: 50
    txn_base_fee_cap_gwei: 1
    txn_require_curve_quote: false
```

Declare the original public addresses under `managed_signers.scan` and
`managed_signers.kick` (or `MANAGED_SIGNERS` as JSON). Both profiles currently
share the same Tidal key. `check-config` verifies these declarations by unlocking
the selected encrypted keystore without opening the database or contacting RPC.

The private secret file contains `RPC_URL`, `TXN_KEYSTORE_PATH`,
`TXN_KEYSTORE_PASSPHRASE` and any configured provider/notification credentials.
Keep the file and original encrypted key private and recoverable. Local CLI
commands do not use `TIDAL_API_BASE_URL` or `TIDAL_API_KEY`.

Current-state reads must be fresh; default head/finalized age limits are 180 and
1800 seconds. Prepared conditions expire after 300 seconds. Cached candidate
data defaults to a 1200-second limit. The no-fill policy retains retry delays
of 720 and 1440 minutes. Provider failure can defer an action without blocking
basic database validation or the restored read-only API.

`TIDAL_API_HOST` and `TIDAL_API_PORT` default to `0.0.0.0:8787`; production can
bind loopback behind its existing reverse proxy. Use `TIDAL_API_CORS_ALLOWED_ORIGINS`
for explicitly allowed browser origins. Browser builds may receive only public
API URL configuration, never provider credentials or signing secrets.
