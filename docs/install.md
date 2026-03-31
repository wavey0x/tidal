# Install

This is the setup source of truth for Tidal. Start here whether you are installing the CLI client, running the server, or working from a repo checkout.

## Choose A Path

- `CLI client`: the common case. Use `tidal` against a hosted or self-hosted API and keep signing local.
- `Server operator`: run `tidal-server`, the scanner, the API, and the shared SQLite database.
- `Contributor`: develop from a repo checkout with `uv run`.

## Before You Start

Install [`uv`](https://docs.astral.sh/uv/) first, then choose the path that matches your role.

For tool installs, prefer Python 3.12 explicitly. `uv tool install` can otherwise choose a newer
managed interpreter than the repo is actively exercised on.

## What `tidal init` Creates

`tidal init` scaffolds:

- `~/.tidal/config.yaml`
- `~/.tidal/.env`
- `~/.tidal/state/`
- `~/.tidal/state/operator/`
- `~/.tidal/run/`

This scaffold is client-focused:

- API client settings first
- local execution defaults next
- workstation execution settings after that

## What `tidal-server init-config` Creates

`tidal-server init-config` scaffolds tracked server config files in `./config/` by default:

- `config/server.yaml`
- `config/.env.example`

## CLI Client Install

Use this when you want the `tidal` CLI client on a workstation that talks to a remote or hosted API.

```bash
uv python install 3.12
uv tool install --python 3.12 git+ssh://git@github.com/wavey0x/tidal.git
uv tool update-shell
tidal init
```

Then review:

- `~/.tidal/.env`: set `TIDAL_API_KEY`, plus keystore secrets if you will broadcast locally
- `~/.tidal/config.yaml`: confirm `tidal_api_base_url`

Minimum client setup:

```bash
TIDAL_API_KEY=<cli-client-api-key>
```

If you are using the hosted API at `https://api.tidal.wavey.info`, API keys are provided by wavey on request.

The generated `config.yaml` already defaults `tidal_api_base_url` to the hosted API. If you are pointing at a different server, override that value there or pass `--api-base-url` per command.

Verify the install:

```bash
tidal --help
tidal kick inspect
```

## Upgrade An Existing Tool Install

To pull the latest Tidal:

```bash
uv tool install --reinstall git+ssh://git@github.com/wavey0x/tidal.git
```

If it pauses, it may be waiting for your SSH key passphrase.

## Server Operator Install

Use this on the machine that owns the shared database, scanner, and API.

```bash
git clone git@github.com:wavey0x/tidal.git
cd tidal
uv sync --extra dev
uv run tidal-server init-config
```

Then review:

- `config/server.yaml`: authoritative server runtime and kick policy
- `config/.env.example`: documented server secrets
- `config/.env` or `TIDAL_ENV_FILE`: actual server secrets such as `RPC_URL`
- `TIDAL_HOME=/var/lib/tidal` or another non-repo state location

Minimum server operator bootstrap:

```bash
uv run tidal-server db migrate --config config/server.yaml
uv run tidal-server scan run --config config/server.yaml
uv run tidal-server api serve --config config/server.yaml
```

For a self-hosted server, create client API keys with `tidal-server auth create --label <name>`.

## Contributor Install From A Repo Checkout

Use this when you are developing Tidal from source instead of installing it as a tool.

```bash
git clone git@github.com:wavey0x/tidal.git
cd tidal
uv sync --extra dev
uv run tidal init
uv run tidal-server init-config
```

If you are troubleshooting a hanging tool install on a server, rerun with verbose logs so `uv`
prints the last package it is preparing:

```bash
uv tool install --python 3.12 -v git+ssh://git@github.com/wavey0x/tidal.git
```

Then use `uv run` for Python-side commands from the checkout:

```bash
uv run tidal-server db migrate --config config/server.yaml
uv run tidal-server scan run --config config/server.yaml
uv run tidal-server api serve --config config/server.yaml
uv run pytest
uv run mkdocs serve
```

## Where To Go Next

- CLI client usage: [CLI Client Guide](operator-guide.md)
- Server hosting and operations: [Server Operator Guide](server-ops.md)
- Contributor workflow: [Local Development](local-dev.md)
- Exact command docs: [CLI Command Map](cli-reference.md)
- Settings reference: [Configuration](config.md)
