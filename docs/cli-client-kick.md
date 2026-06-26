# CLI Client: `tidal kick`

`tidal kick` is the main CLI client workflow for inspecting and executing kick candidates through the control-plane API.

## Subcommands

- `inspect`: show the current shortlist and why entries are ready or deferred
- `run`: prepare candidates one at a time and send them after review

## Common Invocations

Inspect the current shortlist:

```bash
tidal kick inspect
```

Focus on fee burners:

```bash
tidal kick inspect --source-type fee-burner
```

Run interactively with confirmation:

```bash
tidal kick run
```

Run unattended:

```bash
tidal kick run --no-confirmation
```

Run headless for service automation:

```bash
tidal kick run --headless
```

Allow prepares to continue when Curve quoting is unavailable:

```bash
tidal kick run --no-require-curve
```

Bypass the persisted killed-gauge guard for a one-off manual run:

```bash
tidal kick run --allow-killed-gauge
```

Lower the candidate USD threshold for a one-off run:

```bash
tidal kick run --min-usd-value 200
```

Skip sends while mainnet base fee is above 5 gwei:

```bash
tidal kick run --max-base-fee-gwei 5
```

## Important Flags

- `--source-type`: filter to `strategy` or `fee-burner`
- `--source`: target one source address
- `--auction`: target one auction address
- `--limit`: cap how many candidates are considered
- `--min-usd-value`: override `txn_usd_threshold` for candidate selection in this inspect or run
- `--max-base-fee-gwei`: override the send-time base-fee cap for this run
- `--show-all`: include non-ready entries on `inspect`
- `--no-confirmation`: skip the interactive confirmation prompt
- `--headless`: skip confirmation, emit compact line logs, drain the current ready set, and return success for normal no-op outcomes on `run`
- `--verbose`: show more prepare and skip detail on `run`
- `--require-curve` and `--no-require-curve`: tighten or relax fresh quote requirements for that run
- `--allow-killed-gauge`: bypass a persisted killed Curve gauge status for this manual run
- `--json`: emit machine-readable output for `inspect`

Signing defaults to `TXN_KEYSTORE_PATH` and `TXN_KEYSTORE_PASSPHRASE`. Use `--keystore` and `--password-file` only when you need a one-off override. The sender address is inferred from the resolved keystore.

## How `run` Behaves

The client does not precompute and send a whole batch at once. Instead it repeats this loop:

1. fetch the current shortlist from the API
2. prepare the next exact candidate
3. show a review panel unless `--headless` is used
4. sign and send locally if confirmed, or automatically when `--headless`/`--no-confirmation` is used
5. report broadcast and receipt data back to the API

That keeps the final transaction payload aligned with the latest on-chain state.
If a prepared transaction sits longer than `prepared_action_max_age_seconds`, the client skips it instead of sending stale quotes.
If the current base fee is above `txn_base_fee_cap_gwei` or `--max-base-fee-gwei`, the client skips that candidate instead of signing or broadcasting.
With `--headless`, the client keeps preparing and sending the current ready queue until it is cleared, skipped, or blocked. Normal no-op outcomes such as no ready candidates or prepare-time skips exit successfully for timer use.

## Review And Warning Notes

The confirmation view typically shows:

- the auction being kicked
- the token pair
- the current quote and pricing profile
- the sender and gas estimate for the outbound transaction

For some kicks, the client also shows a live quote warning when the just-in-time quote is materially above the evaluated spot output used during shortlist ranking.

See [Pricing](pricing.md) and [Kick Selection](kick-selection.md) for the underlying logic.
