# Tidal

Tidal discovers Yearn strategy and fee-burner inventory, operates auctions and
serves a monitoring dashboard. One local runtime owns one SQLite database and
one managed transaction ledger. Operators run native commands on the host over
SSH; the API serves read-only data and stateless unsigned previews.

```text
scanner ────────────> SQLite ────────────> read-only API ──> dashboard
                         ↑
local commands ──> shared managed sender ──> Ethereum
                         ↑
                retained transaction identity
```

Every managed send commits its exact identity, unsigned intent and business
links before broadcasting once. Pending and unfinalized attempts block the
signer. Canonical finalized evidence is required before business outcomes change.
There is no remote operator outbox, receipt-report protocol or background API
receipt worker.

## Start here

- [Installation](docs/install.md) and [local development](docs/local-dev.md).
- [Operator guide](docs/operator-guide.md) and [command reference](docs/cli-reference.md).
- [Backup, legacy migration and recovery](docs/recovery.md).
- [Architecture](docs/architecture.md), [configuration](docs/config.md) and [API](docs/api-reference.md).
- [Pricing](docs/pricing.md) and [kick selection](docs/kick-selection.md).

For a deliberately new development database:

```bash
uv sync --frozen --extra dev
uv run tidal init
uv run tidal db init --config config/server.yaml
uv run tidal api serve --config config/server.yaml
```

All commands use `config/server.yaml` or an explicit `--config`, and one selected
secret file (`~/.tidal/server/.env` by default, overridden by `TIDAL_ENV_FILE`).
Execution starts held. Existing databases require explicit migration or restore;
services never initialize or migrate state at startup. `tidal-server` remains a
compatibility entry point into the same runtime.

Production recovery uses a retained Linux release built by
`scripts/build_release.py`, with Python, SQLite, pinned wheels and the built UI.
`scripts/prepare_release.py` installs it offline. Retain the original encrypted
key, effective configuration and a verified native SQLite snapshot together.
Keep activation local and recreate it only through explicit `tidal resume`.

## Repository

`tidal/scanner/` owns observation, `tidal/transaction_service/` owns auction
policy/preparation, `tidal/execution.py` owns managed signing and submission,
`tidal/transactions.py` owns reconciliation, and `tidal/recovery.py` exposes
native recovery operations. `tidal/api/` and `tidal/read/` share read models.
`ui/` contains the dashboard; `contracts/` contains the AuctionKicker contracts.
