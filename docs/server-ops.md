# Server operations

All services and SSH commands use the same immutable release, configuration and
secret file. `tidal-server` is a compatibility alias for server commands in that
runtime. The maintained database is the existing `tidal.db`; execution locking
and activation live outside its replaceable directory under `TIDAL_HOME`.

The existing service layout remains an API, a scanner timer and a kick timer:

```bash
tidal api serve --config /path/to/server.yaml
tidal scan run --config /path/to/server.yaml --no-confirmation --auto-settle --auto-enable-tokens
tidal kick run --config /path/to/server.yaml --headless --wait-seconds 2700
```

The hourly kick service evaluates both source profiles in one bounded cycle.
It retries temporary execution contention and waits for retained transactions
to finalize before preparing the next source. The 45-minute budget is shared
across sources; the service timeout is 50 minutes. It releases the execution
lock and database transaction between checks. Review-required attempts and
other policy blockers remain blocked. A source that submitted gets one pass
per cycle; the last source to submit is considered second next hour, using
the existing transaction ledger. Each profile retains its own thresholds,
gas limits and quote requirements.

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
