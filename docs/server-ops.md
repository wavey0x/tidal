# Server operations

All services and SSH commands use the same immutable release, configuration and
secret file. `tidal-server` is a compatibility alias for server commands in that
runtime. The maintained database is the existing `tidal.db`; execution locking
and activation live outside its replaceable directory under `TIDAL_HOME`.

The existing service layout remains an API, a scanner timer and a kick timer:

```bash
tidal api serve --config /path/to/server.yaml
tidal scan run --config /path/to/server.yaml --no-confirmation --auto-settle --auto-enable-tokens
tidal kick run --config /path/to/server.yaml --headless
```

The kick service evaluates both source profiles once and exits. Its timer
starts another pass one minute after the previous pass finishes, so runs do
not overlap. Lock contention and unresolved transactions return exit code 75,
which the service treats as a normal wait until the next tick. Each pass
reconciles retained transactions before preparing fresh candidates. The last
source to submit is considered second on the next pass, using the existing
transaction ledger. Review-required attempts stay blocked; no transaction is
automatically resent.

Checking every minute does not override the configured lot caps, cooldowns,
gas limits or quote requirements. The usual 24-hour cooldown still applies;
explicit shorter cooldowns can now take effect between hourly boundaries.

The scanner uses `TIDAL_HOME/scan.lock` to prevent overlapping scans.
Observation does not hold `execution.lock`; reconciliation, settlement and
token enablement take that lock for their complete execution stages. A busy
execution stage is deferred while observation continues. Deployment and
recovery still stop all database users before replacing state.

Use persistent service holds for deployment and restore, including every old
writer. Remove automatic startup migrations. The API can serve the restored
database while signing and external dependencies remain held.

`tidal check-config --json` validates the original encrypted key and declared
policies without opening the database or contacting RPC. `tidal db check --json`
checks schema, identity and integrity offline. `tidal status --json` distinguishes
API readiness, runtime identity, chain readiness, current observations, cached
prices and per-signer execution readiness.

For database replacement, API credential rotation, notification baselining,
native reconciliation and explicit resumption, follow [backup and recovery](recovery.md).
Price availability can gate a trading decision without preventing the basic
restored API from starting. No-work and temporary waiting results are valid
operational states; inspect structured blockers before changing policy.
