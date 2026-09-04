# CLI Reference

This is the single command-map page for Tidal. Use it to find the exact command page quickly.

Setup and workflow guidance live elsewhere:

- setup: [Install](install.md)
- CLI-client workflows: [CLI Client Guide](operator-guide.md)
- server workflows: [Server Operator Guide](server-ops.md)

## Shared Patterns

### Confirmation first

Mutating transaction commands send transactions after review by default. Use `--no-confirmation` only for unattended or machine-driven execution.
Use `tidal kick run --headless` for timer-driven kick execution that needs plain logs and successful no-op exits.

### Local signing

Broadcasting commands resolve the signer from `TXN_KEYSTORE_PATH` and `TXN_KEYSTORE_PASSPHRASE` by default. Use `--keystore` and `--password-file` only for one-off overrides. The sender is inferred from the resolved keystore.

The private key stays with the machine running the CLI.

### Machine-readable output

Read and export-oriented commands may accept `--json` for scripting. Kick execution uses `--headless` for automation logs instead of JSON output.

Auction execution (`--json` or text) and kick execution report receipt-derived
`confirmed`, `pending`, `failed`, or `partial` outcomes. Exit codes are 0 for fully
confirmed execution, 4 for failed or still-pending execution, and 5 for partial
execution. A normal no-op exits 2, except timer-oriented `kick run --headless`,
which retains its successful no-op exit of 0. Pending is not confirmation: check
the retained hash before retrying. Auction JSON contains only structured output,
including the execution counts; preparation success is not execution success.

### Config overrides

Both executables support path overrides such as `--config` and the `TIDAL_*` environment variables documented in [Configuration](config.md).

## Choose The Right CLI

Use `tidal` when:

- you are a remote operator calling the hosted or self-hosted API
- you want the server to own shared state and audit history
- you want wallet signing to stay local to your workstation
- you want kick, auction, or log workflows, including from the server host itself

Use `tidal-server` when:

- you are operating the host that owns the shared database
- you are running the scanner, API, or auth management

## CLI Client Commands

| Command | Use it when | Reference |
|---|---|---|
| `tidal init` | You are bootstrapping a workstation or refreshing scaffold files | [CLI Client: `tidal init`](cli-client-init.md) |
| `tidal kick` | You want to inspect or execute kick candidates through the API | [CLI Client: `tidal kick`](cli-client-kick.md) |
| `tidal auction` | You want to deploy, enable, or settle auctions through the API | [CLI Client: `tidal auction`](cli-client-auction.md) |
| `tidal logs` | You want historical kick and scan data from the API | [CLI Client: `tidal logs`](cli-client-logs.md) |

## Server Operator Commands

| Command | Use it when | Reference |
|---|---|---|
| `tidal-server db` | You need to apply migrations | [Server Operator: `tidal-server db`](cli-server-db.md) |
| `tidal-server scan` | You need to run one scan cycle or an explicit scan-side auction maintenance pass | [Server Operator: `tidal-server scan`](cli-server-scan.md) |
| `tidal-server api` | You need to serve the FastAPI control plane | [Server Operator: `tidal-server api`](cli-server-api.md) |
| `tidal-server auth` | You need to create, list, or revoke API keys | [Server Operator: `tidal-server auth`](cli-server-auth.md) |
