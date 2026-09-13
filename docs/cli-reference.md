# CLI reference

Run on the application host using its one release and configuration. Use
`tidal COMMAND --help` for exact options. There is no API client mode.

| Command | Purpose |
|---|---|
| `tidal init` | Scaffold one configuration and secret-file example |
| `tidal check-config --json` | Validate policy and the original encrypted key offline |
| `tidal db init` | Explicitly create empty state, held |
| `tidal db check --json` | Offline schema, UUID, integrity and foreign-key check |
| `tidal db migrate --json` | Held migration; legacy sources required for consolidation |
| `tidal db snapshot --output FILE --json` | Publish a coherent verified SQLite backup |
| `tidal db prepare-restore --credential-file FILE --json` | Rotate API access, stale prices and baseline notifications |
| `tidal hold --json` | Revoke native activation |
| `tidal reconcile --json` | Check retained transactions and bounded known rounds |
| `tidal refresh --recovery --json` | Silent current-state observation |
| `tidal status --json` | Inspect independent readiness conditions |
| `tidal resume --json` | Activate checked identities and current nonce |
| `tidal scan run` | Normal observation and optional managed maintenance |
| `tidal kick inspect` / `run` | Local policy inspection, preparation and execution |
| `tidal auction enable-tokens` / `settle` / `sweep` | Native managed auction operations |
| `tidal logs kicks` / `scans` / `show` | Read local business history |
| `tidal api serve` | Start the read-only API |
| `tidal auth create` / `list` / `revoke` | Manage API access locally |

Operational JSON uses `interface_version`, `code`, `data`, `blockers` and
`warnings`. Success exits 0; temporary busy/waiting outcomes exit 75; other
blockers exit 1. API liveness, price readiness and permission to send are
different conditions. Native resume does not enable systemd timers.

See [operator workflows](operator-guide.md) and [recovery](recovery.md) for
ordering, migration prerequisites, pending transactions and scoped repair.
