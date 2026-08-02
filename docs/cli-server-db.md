# Server Operator: `tidal-server db`

`tidal-server db` is the database maintenance entry point.

## Subcommands

- `migrate`: apply the current Alembic schema migrations
- `repair-auction-rounds`: audit current active, non-ignored auction automation;
  add `--apply` to reconcile its receipts and repair deterministic links

## Common Invocation

```bash
tidal-server db migrate --config config/server.yaml
tidal-server db repair-auction-rounds --config config/server.yaml
```

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
  are stopped. Inactive, retired, and ignored historical pairs are reported as
  `OUT_OF_SCOPE`; they do not block the audit. Follow apply mode with check mode
  and do not resume automation unless the audit passes.
