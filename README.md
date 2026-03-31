# Tidal

Tidal is Yearn's auction operations stack. It scans strategy and fee-burner inventories, caches balances and token prices in SQLite, prepares auction actions through a control-plane API, supports local transaction signing from a CLI client, and exposes a dashboard for monitoring the resulting state.

Documentation lives in [`docs/`](./docs/index.md). The intended hosted docs domain is `https://docs.tidal.wavey.info`.

## Production Components

| Component | Role | Entry point |
|---|---|---|
| `tidal-server` | Server operator CLI for migrations, scans, kick daemons, API serving, and API key management | `tidal.server_cli:app` |
| `tidal` | CLI client for API-backed inspection, preparation, signing, broadcast, and log inspection | `tidal.cli:app` |
| `ui/` | React dashboard for strategies, fee burners, logs, and CLI client actions | `ui/src/App.jsx` |
| `contracts/` | Foundry project for the on-chain `AuctionKicker` helper contract | `contracts/src/AuctionKicker.sol` |

## System Shape

```text
scanner -> SQLite -> FastAPI control plane -> dashboard UI
                                  ^
                                  |
                       CLI client prepare/read calls
                                  |
                           local wallet signing
                                  |
                               Ethereum
```

The server owns the database, scans, API, and audit history. CLI clients keep private keys local: the CLI asks the API to prepare actions, signs transactions locally, broadcasts them, and reports broadcast/receipt data back to the API.

## Quick Start

### Backend contributor

```bash
uv sync --extra dev
uv run tidal init
uv run tidal-server init-config
uv run tidal-server db migrate --config config/server.yaml
uv run tidal-server scan run --config config/server.yaml
uv run tidal-server api serve --config config/server.yaml
```

Required setup:

- Run `uv run tidal init` to scaffold client files under `~/.tidal/`.
- Run `uv run tidal-server init-config` to scaffold tracked server files under `config/`.
- Put client secrets in `~/.tidal/.env`.
- Put server secrets in `config/.env` for local repo use, or point `TIDAL_ENV_FILE` at a path outside Git.
- Put authoritative server runtime and kick policy in `config/server.yaml`.
- If you want the UI locally, run `cd ui && npm install && npm run dev`.

### CLI client

```bash
export TIDAL_API_BASE_URL=https://api.tidal.wavey.info
export TIDAL_API_KEY=<cli-client-api-key>

tidal kick inspect
tidal kick run
tidal kick run --broadcast --sender <address> --account <foundry-keystore-name>
```

For the hosted API at `https://api.tidal.wavey.info`, API keys are provided by wavey on request.

Broadcasting commands use a Foundry-style wallet surface: `--sender`, `--account`, `--keystore`, and `--password-file`.

To upgrade an existing tool install to the latest Tidal:

```bash
uv tool install --reinstall git+ssh://git@github.com/wavey0x/tidal.git
```

## Repository Map

- [`tidal/scanner/`](./tidal/scanner/) discovers strategies, fee burners, balances, and auction mappings, then refreshes cached token metadata and prices.
- [`tidal/transaction_service/`](./tidal/transaction_service/) shortlists kick candidates, prepares actions, prices lots, and records transaction results.
- [`tidal/api/`](./tidal/api/) serves the FastAPI control plane at `/api/v1/tidal`.
- [`tidal/read/`](./tidal/read/) exposes read models for dashboard rows, logs, runs, and action history.
- [`tidal/persistence/`](./tidal/persistence/) plus [`alembic/`](./alembic/) define the shared SQLite schema and migrations.
- [`ui/`](./ui/) contains the React dashboard and Vercel configuration.
- [`contracts/`](./contracts/) contains the Foundry contract, scripts, and tests for `AuctionKicker`.
- [`tests/`](./tests/) contains unit, integration, and fork coverage.

## Where To Go Next

- Start with the docs landing page: [`docs/index.md`](./docs/index.md)
- Quick install guide: [`docs/install.md`](./docs/install.md)
- System overview: [`docs/architecture.md`](./docs/architecture.md)
- Local development: [`docs/local-dev.md`](./docs/local-dev.md)
- CLI client guide: [`docs/operator-guide.md`](./docs/operator-guide.md)
- Server operator guide: [`docs/server-ops.md`](./docs/server-ops.md)
- CLI command map: [`docs/cli-reference.md`](./docs/cli-reference.md)
- API reference: [`docs/api-reference.md`](./docs/api-reference.md)
- Configuration reference: [`docs/config.md`](./docs/config.md)

## Code Entry Points

- Scanner: [`tidal/scanner/service.py`](./tidal/scanner/service.py)
- Kick engine: [`tidal/transaction_service/service.py`](./tidal/transaction_service/service.py)
- Kick shortlist logic: [`tidal/transaction_service/evaluator.py`](./tidal/transaction_service/evaluator.py)
- FastAPI app: [`tidal/api/app.py`](./tidal/api/app.py)
- CLI client: [`tidal/cli.py`](./tidal/cli.py)
- Server operator CLI: [`tidal/server_cli.py`](./tidal/server_cli.py)
- Dashboard UI: [`ui/src/App.jsx`](./ui/src/App.jsx)
- Contract: [`contracts/src/AuctionKicker.sol`](./contracts/src/AuctionKicker.sol)
