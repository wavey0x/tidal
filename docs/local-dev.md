# Local Development

This guide is for contributors and local server operators working from a repo checkout.

## Prerequisites

- Python 3.12+
- Node.js 20+ for the dashboard UI
- Foundry for contract tests and deployment scripts
- A mainnet RPC URL

## Backend Setup

```bash
uv sync --extra dev
```

This creates the local project environment from `uv.lock`, installs the package, and includes the `dev` extra for tests and docs work.

Use `uv run ...` for Python-side commands from the checkout instead of activating a manual venv.

## Secrets And Config

Initialize the local config home first:

```bash
uv run tidal init
```

Put secrets in `~/.tidal/.env`:

```bash
RPC_URL=https://...
TOKEN_PRICE_AGG_KEY=...
TIDAL_API_KEY=...
```

Put operational settings in `~/.tidal/config.yaml`. The scaffold written by `tidal init` already includes:

- SQLite path
- role-labeled sections for shared defaults, server operator settings, and CLI client convenience values
- scanner and API server defaults
- transaction guardrails

Settings precedence is:

```text
environment variables > ~/.tidal/config.yaml > Python defaults
```

See [Configuration](config.md) for the full schema.

## Initialize The Database

```bash
uv run tidal-server db migrate
```

This applies Alembic migrations to the configured SQLite database.

## Create An API Key

If you want to exercise authenticated API flows locally:

```bash
uv run tidal-server auth create --label yourname
```

The command prints a plaintext key once. Keep it somewhere safe, then export it:

```bash
export TIDAL_API_KEY=<printed-key>
```

## Run The Backend

Run one scan:

```bash
uv run tidal-server scan run
```

Start the API:

```bash
uv run tidal-server api serve
```

By default the API listens on `0.0.0.0:8787`. Override with `TIDAL_API_HOST` and `TIDAL_API_PORT` if needed.

## Use The CLI Client Against Local API

```bash
export TIDAL_API_BASE_URL=http://127.0.0.1:8787

uv run tidal kick inspect
uv run tidal logs kicks
uv run tidal kick run
```

For broadcast flows you also need wallet flags such as:

- `--sender`
- `--account`
- `--keystore`
- `--password-file`

## Run The Dashboard UI

```bash
cd ui
npm install
TIDAL_API_PROXY_TARGET=http://127.0.0.1:8787 npm run dev
```

Then open `http://localhost:5173`.

You can also point directly at a deployed API:

```bash
VITE_TIDAL_API_BASE_URL=https://api.tidal.wavey.info/api/v1/tidal npm run dev
```

If you want authenticated UI actions locally:

```bash
VITE_TIDAL_API_KEY=$TIDAL_API_KEY npm run dev
```

## Run Tests

Python tests:

```bash
uv run pytest
```

You can also scope to unit, integration, or fork tests:

```bash
uv run pytest tests/unit
uv run pytest tests/integration
uv run pytest tests/fork
```

Contract tests:

```bash
cd contracts
MAINNET_URL=$RPC_URL forge test -vvv
```

## Preview The Docs Site

```bash
uv run mkdocs serve
```

The local docs site will be available at `http://127.0.0.1:8000`.

## Recommended First Session

If you are new to the repo, the fastest way to build context is:

1. Run `uv run tidal-server db migrate`
2. Run `uv run tidal-server scan run`
3. Run `uv run tidal-server api serve`
4. Open the UI locally
5. Run `uv run tidal kick inspect`
6. Read [Architecture](architecture.md) and [Kick Selection](kick-selection.md)
