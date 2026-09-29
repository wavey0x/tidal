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

The scheduled service uses `--headless` for one pass through both sources.
It exits with code 75 when execution is busy or a retained transaction remains
unresolved. The timer starts the next pass one minute after completion; the
command does not sleep or retry internally. Headless live runs consider the
last submitting source second, using the retained ledger so restart does not
reset its turn. Finality and review checks remain in force, and previously
submitted transactions are never resent.

Filter with `--source`, `--auction`, `--token` and `--limit`. Explicit
`--min-usd-value`, `--max-base-fee-gwei` and `--require-curve/--no-require-curve`
override the selected execution profile. `--batch` combines compatible kicks;
it does not bypass the one-unresolved-attempt-per-signer rule.

`--allow-no-fill-retry` requires an exact auction/token pair and is rejected with
`--headless`. It is a deliberate policy override, not a recovery retry mechanism.
See [operator policy](operator-guide.md), [kick selection](kick-selection.md)
and [recovery](recovery.md). There are no API URL/key flags or remote outbox.

Clear the current cooldown for selected auction/token pairs so the scheduled
runner can process them normally:

```bash
tidal kick clear-cooldown --auction 0xAUCTION --token 0xTOKEN1 --token 0xTOKEN2
```

The command applies immediately, needs no signer or RPC, and supports `--json`
for automation. Add `--dry-run` for a read-only preview. Repeated clears are
harmless; pairs without an active cooldown are unchanged. The clear is recorded
on the latest kick, survives restarts and gas/quote skips, and never changes its
transaction history. A new kick starts its own normal cooldown. All other
eligibility and execution checks still apply; this command does not submit a kick.
