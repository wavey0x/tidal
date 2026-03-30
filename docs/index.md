# Tidal Docs

Tidal is Yearn's auction operations stack. It scans strategy and fee-burner inventories, caches balances and prices in SQLite, exposes that state through a FastAPI control plane, and lets CLI clients prepare and broadcast auction transactions with local wallet signing.

## Read This First

- Installing the CLI client or server operator: go to [Install](install.md)
- New to the system: start with [Architecture](architecture.md)
- Setting up a workstation: go to [Local Development](local-dev.md)
- Operating as a CLI client against a remote API: go to [CLI Client Guide](operator-guide.md)
- Running the server and daemons: go to [Server Operator Guide](server-ops.md)
- Understanding config files and role-specific settings: go to [Configuration](config.md)

## System At A Glance

| Component | What it does | Main code |
|---|---|---|
| Scanner | Discovers strategies, fee burners, balances, prices, and auction mappings | `tidal/scanner/` |
| Transaction service | Shortlists candidates, prepares kicks, computes lot pricing, and records results | `tidal/transaction_service/` |
| Control plane API | Serves dashboard/log data and prepares action payloads | `tidal/api/` |
| CLI client | Calls the API, signs locally, broadcasts locally, and reports receipts | `tidal/cli.py` |
| Dashboard UI | Displays cached state and logs, and drives CLI client actions | `ui/` |
| Contracts | Foundry project for the on-chain `AuctionKicker` helper | `contracts/` |

## Reading Paths

### Backend contributor

1. [Install](install.md)
2. [Local Development](local-dev.md)
3. [Architecture](architecture.md)
4. [Configuration](config.md)
5. [Pricing](pricing.md)
6. [Kick Selection](kick-selection.md)

### CLI client

1. [Install](install.md)
2. [CLI Client Guide](operator-guide.md)
3. [CLI Client Overview](cli-client-reference.md)
4. [CLI Client: `tidal kick`](cli-client-kick.md)
5. [Configuration](config.md)
6. [Pricing](pricing.md)

### Server operator

1. [Install](install.md)
2. [Server Operator Guide](server-ops.md)
3. [Server Operator CLI Overview](cli-server-reference.md)
4. [Configuration](config.md)
5. [API Reference](api-reference.md)

## Reference

- [Install](install.md)
- [CLI Overview](cli-reference.md)
- [CLI Client Overview](cli-client-reference.md)
- [Server Operator CLI Overview](cli-server-reference.md)
- [API Reference](api-reference.md)
- [Configuration](config.md)
- [Glossary](glossary.md)

## Source Of Truth

These docs are meant to explain the current system, not preserve historical plans. When behavior disagrees with prose, prefer the code:

- CLI surface: `tidal --help` and `tidal-server --help`
- API surface: FastAPI routes in `tidal/api/routes/`
- Config schema: `tidal/config.py`
