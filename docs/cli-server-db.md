# Server Operator: `tidal-server db`

`tidal-server db` is the database maintenance entry point.

## Subcommands

- `migrate`: apply the current Alembic schema migrations
- `repair-auction-rounds`: audit every retained auction round; add `--apply` to
  replay receipts, discover settlement logs, rebuild links, and baseline
  inactive historical evidence the chain can no longer prove

## Common Invocation

```bash
tidal-server db migrate --config config/server.yaml
tidal-server db repair-auction-rounds --config config/server.yaml
```

For the one-time full repair:

```bash
tidal-server db repair-auction-rounds --apply --config config/server.yaml
tidal-server db repair-auction-rounds --apply --config config/server.yaml
```

The second apply must report `Mutations: 0` before automation resumes.

## When To Run It

Run migrations:

- during first-time bootstrap
- after upgrading the installed package
- as an `ExecStartPre=` step before API or scanner startup

## Notes

- `migrate` is safe to run repeatedly.
- It does not require `RPC_URL`.
- It operates on the database path resolved from `config/server.yaml` and any `TIDAL_*` path overrides.
- Run `repair-auction-rounds --apply` only while API, scanner, and kick scheduling
  are stopped.
- Back up the SQLite database and run `PRAGMA integrity_check` first.
- Apply mode covers retained history without shortlist, activity, threshold, or
  ignore filtering. Each transaction receipt is replayed once and settlement
  logs are scanned once per auction.
- Apply mode may mark an unprovable round as a reviewed historical baseline only
  when a later round superseded it or the exact auction/token pair is inactive.
  Runtime reconciliation never creates baselines.
- Active or otherwise unresolved current evidence continues to fail the audit.
