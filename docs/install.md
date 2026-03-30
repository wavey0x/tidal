# Install

This page is the fast path for getting Tidal onto a machine with the current `uv`-based workflow.

## Before You Start

Install [`uv`](https://docs.astral.sh/uv/) first, then choose the path that matches your role.

## CLI Client Install

Use this when you want the `tidal` CLI client on a workstation that talks to a remote or hosted API.

```bash
uv tool install git+ssh://git@github.com/wavey0x/tidal.git
uv tool update-shell
tidal init
```

Then edit:

- `~/.tidal/.env`
- `~/.tidal/config.yaml`

Minimum client setup:

```bash
TIDAL_API_KEY=<cli-client-api-key>
```

The generated `config.yaml` already defaults `tidal_api_base_url` to the hosted API. If you are pointing at a different server, override that value there or pass `--api-base-url` per command.

Verify the install:

```bash
tidal --help
tidal kick inspect
```

## Server Operator Install

Use this on the machine that owns the shared database, scanner, and API.

```bash
uv tool install git+ssh://git@github.com/wavey0x/tidal.git
uv tool update-shell
tidal init
```

Then edit:

- `~/.tidal/.env`
- `~/.tidal/config.yaml`
- `~/.tidal/auction_pricing_policy.yaml` if you need pricing overrides

Minimum server operator bootstrap:

```bash
tidal-server db migrate
tidal-server scan run
tidal-server api serve
```

At minimum, `~/.tidal/.env` needs `RPC_URL`. Most production installs will also set secrets such as `TOKEN_PRICE_AGG_KEY`.

## Contributor Install From A Repo Checkout

Use this when you are developing Tidal from source instead of installing it as a tool.

```bash
git clone git@github.com:wavey0x/tidal.git
cd tidal
uv sync --extra dev
uv run tidal init
```

Then use `uv run` for Python-side commands from the checkout:

```bash
uv run tidal-server db migrate
uv run tidal-server scan run
uv run tidal-server api serve
uv run pytest
uv run mkdocs serve
```

## Where To Go Next

- CLI client usage: [CLI Client Guide](operator-guide.md)
- Server hosting and operations: [Server Operator Guide](server-ops.md)
- Contributor workflow: [Local Development](local-dev.md)
- Settings reference: [Configuration](config.md)
