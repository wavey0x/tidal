# CLI Client: `tidal auction`

`tidal auction` prepares and sends direct auction-management actions through the API.

## Subcommands

- `deploy`: create a new auction from the configured factory
- `enable-tokens`: queue token enable calls for an auction
- `settle`: resolve an active lot when it is settleable

## Common Invocations

Deploy a new auction:

```bash
tidal auction deploy \
  --want 0xWant \
  --receiver 0xReceiver \
  --starting-price 1234
```

Enable discovered tokens for an auction:

```bash
tidal auction enable-tokens 0xAuction
```

For configured fee burners, the API also accepts the fee-burner address or its configured want token and resolves the cached auction mapping.
The `enable-tokens` prepare request uses `txn_max_gas_limit` from `~/.tidal/cli/config.yaml` to decide how many enable calls can be placed in each transaction.

Add one extra token explicitly:

```bash
tidal auction enable-tokens 0xAuction --extra-token 0xToken
tidal auction enable-tokens 0xAuction --extra-token 0xTokenA --extra-token 0xTokenB
```

Settle an auction:

```bash
tidal auction settle 0xAuction
tidal auction settle 0xAuction --token 0xActiveToken
tidal auction settle 0xAuction --sweep
```

Send one of those actions:

```bash
tidal auction settle 0xAuction
```

## Important Flags

Shared across the mutating auction commands:

- `--no-confirmation`
- `--keystore`
- `--password-file`
- `--json` which requires `--no-confirmation`

Subcommand-specific flags:

- `deploy`: `--want`, `--receiver`, `--starting-price`, optional factory and governance overrides
- `enable-tokens`: repeat `--extra-token` to include one or more custom token addresses in discovery
- `settle`: `--token`, `--sweep`

## Behavior Notes

These commands use the same prepare and audit model as `tidal kick`:

1. the API prepares the action and records an audit row
2. the CLI client shows a review panel
3. the CLI client signs and sends locally
4. the CLI client reports the broadcast and receipt back to the API

That means the API owns the action history while the CLI client keeps local control of the wallet.
The signer comes from `TXN_KEYSTORE_PATH` and `TXN_KEYSTORE_PASSPHRASE` by default, or from `--keystore` and `--password-file` if you override them for one command. The sender address is inferred from the resolved keystore.

`--starting-price` is the contract-raw integer. Its units are selected by the
approved factory: v1.0.4 uses a whole want-token amount and v1.0.5 uses a
WAD-scaled want-token amount. The confirmation shows both the decoded economic
amount and the raw integer before signing.
