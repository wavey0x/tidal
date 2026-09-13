# Operator guide

Run `tidal` locally on the application host over SSH, using the same release and
configuration as its services. Commands do not need a remote operator API key.

```bash
tidal status --config /path/to/server.yaml --json
tidal kick inspect --source-type strategy
tidal logs kicks
tidal logs scans
tidal logs show RUN_ID
```

Inspect and log commands do not sign. A dry run can prepare diagnostics without
unlocking a key or sending, but writes local diagnostic history:

```bash
tidal kick run --source-type strategy --dry-run --json
```

After explicit activation, interactive commands show a confirmation before
submission. Scheduled runs use the existing policies:

| Profile | Minimum value | Base fee cap | Curve quote |
|---|---:|---:|---|
| Scanner | $250 | 5 gwei | Required |
| Strategy kick | $100 | 1 gwei | Required |
| Fee-burner kick | $50 | 1 gwei | Optional |

```bash
tidal kick run --source-type strategy
tidal kick run --source-type fee-burner
tidal auction enable-tokens 0xAUCTION
tidal auction settle 0xAUCTION --token 0xTOKEN
tidal auction sweep 0xAUCTION --token 0xTOKEN
```

Only use `--headless` or `--no-confirmation` for deliberately unattended work.
JSON output for live actions requires explicit unattended consent. One-off
policy overrides remain available; use each command's `--help`. Browser wallet
deployment remains available through the UI; the managed CLI does not deploy
auctions.

The shared sender commits transaction identity and all business links before
broadcast. A returned hash or receipt inclusion is not yet final success.
`tidal reconcile` checks retained hashes without resending. A pending attempt
blocks further sends by that signer. Do not clear it because a request timed out.

`tidal hold` removes activation. `tidal resume` checks current dependencies,
signing identity and nonce before activating; it does not start timers. See
[recovery](recovery.md) for service fencing, unknown history and replacement
proof. The no-fill delays remain 720 and 1440 minutes; recovery does not reset
them or rewrite old timestamps.
