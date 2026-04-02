# Configuration

## Role Split

Tidal now has two config homes:

- client config in `~/.tidal/`
- server config in tracked repo files under `config/`

That split is intentional:

- `tidal` is a workstation CLI
- `tidal-server` is the shared execution runtime

## Precedence

Client commands load:

```text
environment variables > ~/.tidal/cli/config.yaml > Python defaults
```

Server commands load:

```text
environment variables > config/server.yaml > Python defaults
```

An explicit `--config` or `TIDAL_CONFIG` override wins in either case.

## Files

| File | Purpose |
|---|---|
| `~/.tidal/cli/config.yaml` | Client-only workstation config for `tidal` |
| `~/.tidal/cli/.env` | Client secrets such as `TIDAL_API_KEY`, `RPC_URL`, and keystore secrets |
| `config/server.yaml` | Tracked server runtime config and kick policy for `tidal-server` |
| `config/.env.example` | Documented server secret names |
| `~/.tidal/server/.env` or `TIDAL_ENV_FILE` | Actual server secrets outside normal Git workflow |

## Client Config

Run `tidal init` to scaffold the client files under `~/.tidal/cli/`.

The client scaffold is intentionally narrow. It is for:

- `tidal_api_base_url`
- `tidal_api_request_timeout_seconds`
- `prepared_action_max_age_seconds`
- local broadcast and fee-preview settings such as:
  `chain_id`, `auction_kicker_address`, `txn_*`, `rpc_timeout_seconds`, `rpc_retry_attempts`

`prepared_action_max_age_seconds` is a CLI-side safety guard. If you wait too long between prepare and send, the client skips that prepared transaction and tells you to re-run.

Normal API-backed workstation use does not need a local kick-policy file anymore.

## Server Config

Run `tidal-server init-config` to scaffold the tracked server files under `config/`.

`config/server.yaml` is the authoritative runtime document for:

- chain and contract wiring that should move with the repo
- monitored fee burners
- server-side transaction execution defaults
- kick pricing, ignore rules, and cooldown policy

Server runtime secrets default to `~/.tidal/server/.env`. For repo-local development, you can also point `TIDAL_ENV_FILE=config/.env`.

Some deployment-wiring values now default in code and do not need `.env` or YAML unless you are overriding them:

- `tidal_api_host = 0.0.0.0`
- `tidal_api_port = 8787`
- `token_price_agg_base_url = https://prices.wavey.info`
- `auctionscan_base_url = https://auctionscan.info`
- `auctionscan_api_base_url = https://auctionscan.info/api`

Most scanner, pricing, multicall, and receipt-reconcile tuning also defaults in code now. Leave those out of the tracked file unless you deliberately need an override through environment variables.

Server mutable files default under `~/.tidal/server/`:

- `tidal.db`
- `action_outbox.db`
- `txn_daemon.lock`

Use `TIDAL_HOME` if you want a different root, for example `/var/lib/tidal`.

## `kick:` Section

Server-side kick policy now lives inside `config/server.yaml` under `kick:`.

Example shape:

```yaml
kick:
  default_profile: volatile

  profiles:
    volatile:
      start_price_buffer_bps: 1000
      min_price_buffer_bps: 500
      step_decay_rate_bps: 25

    stable:
      start_price_buffer_bps: 100
      min_price_buffer_bps: 50
      step_decay_rate_bps: 2

  profile_overrides:
    - auction: "0xAuction"
      token: "0xSellToken"
      profile: stable

  usd_kick_limit:
    "0xToken": 10000

  ignore:
    - source: "0xSource"
    - auction: "0xAuction"
    - auction: "0xAuction"
      token: "0xSellToken"

  cooldown_minutes: 60

  cooldown:
    - auction: "0xAuction"
      token: "0xSellToken"
      minutes: 180
```

`cooldown` applies to the `(auction, token)` pair, not the whole auction or source.

## `monitored_fee_burners`

Server config stores fee burners as:

```yaml
monitored_fee_burners:
  - address: "0x..."
    want_address: "0x..."
    label: "Human name"
```

These entries drive:

- fee-burner balance scanning
- source naming
- fee-burner-to-auction mapping through `(receiver, want)`

## Important Defaults

Current defaults from `tidal/config.py` include:

- `tidal_api_host = 0.0.0.0`
- `tidal_api_port = 8787`
- `token_price_agg_base_url = https://prices.wavey.info`
- `auctionscan_base_url = https://auctionscan.info`
- `auctionscan_api_base_url = https://auctionscan.info/api`
- `scan_concurrency = 20`
- `multicall_auction_batch_calls = 100`
- `rpc_timeout_seconds = 10`
- `price_timeout_seconds = 10`
- `txn_usd_threshold = 100`
- `txn_max_base_fee_gwei = 0.5`
- `txn_max_priority_fee_gwei = 2`
- `txn_quote_spot_warning_threshold_pct = 2`
- `prepared_action_max_age_seconds = 300`
- `cooldown_minutes = 60` in `config/server.yaml`
- `tidal_api_request_timeout_seconds = 30`

Scan auto-settle is not a config setting.
Enable it per invocation with `tidal-server scan run --auto-settle --no-confirmation`.

## Rule Of Thumb

- run `tidal init` on workstations
- run `tidal-server init-config` in the repo checkout
- keep client secrets in `~/.tidal/cli/.env`
- keep server secrets out of Git
- treat `config/server.yaml` as the source of truth for shared runtime behavior
