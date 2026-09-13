# `tidal kick`

Inspect and execute on the application host using its one local runtime and DB.

```bash
tidal kick inspect --source-type strategy
tidal kick inspect --source-type fee-burner
tidal kick run --source-type strategy --dry-run --json
tidal kick run --source-type strategy
tidal kick run --source-type fee-burner --headless
```

`inspect` reads the shortlist. `run` prepares fresh conditions and uses the shared
managed sender. `--dry-run` writes diagnostics without unlocking or sending.
Interactive live runs request confirmation; unattended live JSON requires
`--no-confirmation` or `--headless`.

Filter with `--source`, `--auction`, `--token` and `--limit`. Explicit
`--min-usd-value`, `--max-base-fee-gwei` and `--require-curve/--no-require-curve`
override the selected execution profile. `--batch` combines compatible kicks;
it does not bypass the one-unresolved-attempt-per-signer rule.

`--allow-no-fill-retry` requires an exact auction/token pair and is rejected with
`--headless`. It is a deliberate policy override, not a recovery retry mechanism.
See [operator policy](operator-guide.md), [kick selection](kick-selection.md)
and [recovery](recovery.md). There are no API URL/key flags or remote outbox.
