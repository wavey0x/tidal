# Yearn Rewards Auction Automation (Draft Spec)

Status: Draft (opinionated v0.1)  
Author: Codex + user collaboration  
Target stack: Python 3.12+, `web3.py`, CLI-first service architecture

## 1. Purpose

Build a robust, CLI-driven automation app that:

1. Periodically scans strategy contracts to discover balances of all reward tokens.
2. Prices tokens through a pluggable pricing engine (starting with Curve API).
3. Starts Yearn auctions on-chain with a configurable start-price buffer (default `+10%`).
4. Posts/updates CoW Swap orders as auctions approach clearing conditions.

Dashboard UX is intentionally deferred, but data and APIs must be designed now to support it later.

## 2. Scope

### In Scope (MVP+)

1. Ethereum mainnet support (`chain_id=1`).
2. Strategy scanning and reward-balance snapshot persistence.
3. Pluggable pricing system with first provider: Curve Router API.
4. Auction kickoff automation using Yearn auction registry/factory discovery:
   - Registry: `0x94F44706A61845a4f9e59c4Bc08cEA4503e48D12`
   - `getLatestFactory()` for live factory address discovery.
5. Strategy discovery from Yearn Curve factory:
   - Factory: `0x21b1fc8a52f179757bf555346130bf27c0c2a17a`
   - Enumerate vaults via `allDeployedVautls()` (verify exact ABI spelling at implementation time).
   - Enumerate strategies per vault via `withdrawalQueue(i)` until `address(0)`.
6. CoW Swap order management logic tied to auction state transitions.
7. Production-grade logging, metrics, tracing hooks, and explicit failure/retry behavior.
8. Strong automated tests (unit + integration + mainnet-fork + CLI smoke).

### Out of Scope (for now)

1. Full dashboard UI implementation.
2. Multi-chain expansion beyond Ethereum mainnet.
3. MEV/specialized execution strategies beyond standard RPC + relayer flows.

## 3. Product Requirements

### 3.1 Functional Requirements

1. System must track a configurable set of strategies and their reward tokens.
2. System must persist historical snapshots of reward balances and prices.
3. Pricing engine must support multiple providers with fallback/aggregation policy.
4. Auction kicker must evaluate whether an auction should be started, and start it idempotently.
5. CoW module must evaluate if/when to post orders and update/cancel stale orders.
6. CLI must support one-shot commands and long-running daemon loops.
7. Every critical action (scan, quote, auction-start, order-post) must be observable and auditable.

### 3.2 Non-Functional Requirements

1. Reliability: tolerate transient RPC/API outages with retries and backoff.
2. Safety: fail closed on missing/invalid prices; no auction start without validated quote.
3. Idempotency: repeated runs should not duplicate on-chain or off-chain actions.
4. Traceability: all state transitions and external calls must have correlation IDs.
5. Testability: deterministic tests with mockable provider and chain interfaces.

## 4. High-Level Architecture

```text
                 +-----------------------+
                 |        CLI/API        |
                 | (Typer command layer) |
                 +-----------+-----------+
                             |
                     +-------v--------+
                     | Orchestrators  |
                     | scan/auction   |
                     | cowswap loops  |
                     +---+--------+---+
                         |        |
          +--------------+        +-------------------+
          |                                       |
  +-------v--------+                      +-------v--------+
  | Discovery/Scan |                      | Pricing Engine |
  | strategies/tkn |                      | plugins+policy |
  +-------+--------+                      +-------+--------+
          |                                       |
  +-------v-------------------------------+   +---v------------------+
  |              State Store              |   | External APIs/RPC    |
  | snapshots, prices, auctions, orders   |   | Curve, CoW, Ethereum |
  +----------------+----------------------+   +----------------------+
                   |
            +------v------+
            | Observability|
            | logs/metrics |
            +-------------+
```

## 5. Proposed Repository Layout

```text
tidal/
  DRAFT_SPEC.md
  pyproject.toml
  README.md
  .env.example
  config/
    app.example.yaml
    chains/
      mainnet.yaml
  src/
    tidal/
      __init__.py
      cli.py
      config.py
      logging.py
      types.py
      constants.py
      errors.py
      orchestration/
        scan_loop.py
        auction_loop.py
        cowswap_loop.py
        scheduler.py
      domain/
        strategy.py
        rewards.py
        pricing.py
        auction.py
        order.py
      chain/
        web3_client.py
        contracts/
          loader.py
          auction_registry.py
          auction_factory.py
          auction_instance.py
          erc20.py
      discovery/
        strategy_source.py
        static_source.py
        adapters/
          base.py
          yearn_vault.py
      scanner/
        reward_token_resolver.py
        balance_fetcher.py
        scanner_service.py
      pricing/
        engine.py
        models.py
        providers/
          base.py
          curve.py
      auctioning/
        policy.py
        kicker.py
        monitor.py
      cowswap/
        client.py
        order_builder.py
        poster.py
        policy.py
      persistence/
        db.py
        models.py
        repositories/
          rewards_repo.py
          prices_repo.py
          auctions_repo.py
          orders_repo.py
      observability/
        metrics.py
        tracing.py
        health.py
  tests/
    unit/
    integration/
    fork/
    e2e/
    fixtures/
```

Design intent: keep chain I/O, domain logic, and orchestration separate so each can be tested independently.

## 6. Configuration Model

Use `pydantic-settings` + YAML config + environment overrides.

### 6.1 Global Config

1. `environment`: `dev|staging|prod`
2. `chain_id`: default `1`
3. `rpc_url` (secret via env)
4. `scan_interval_seconds`
5. `auction_check_interval_seconds`
6. `cowswap_check_interval_seconds`
7. `start_price_buffer_bps` (default `1000` = +10%)
8. `reward_threshold_usd` and per-token overrides
9. retry/backoff limits for RPC and HTTP providers

### 6.2 Strategy Config

1. strategy address
2. human-readable name/slug
3. strategy adapter type
4. expected reward tokens (optional; dynamic resolver can add discovered tokens)
5. minimum sell threshold (token units and/or USD)

### 6.3 Key Management

1. signer private key must be env-injected (never committed).
2. optional support for remote signer/HSM later.
3. explicit `dry_run` mode to simulate all actions without sending transactions/orders.

## 7. Data Model (Persistence)

Preferred DB: PostgreSQL for production; SQLite allowed for local dev.

### 7.1 Core Tables

1. `strategies`
   - `address` (PK), `chain_id`, `vault_address`, `name`, `adapter`, `active`, `discovery_metadata_json`, `first_seen_at`, `last_seen_at`
2. `reward_tokens`
   - `address` (PK), `chain_id`, `name`, `symbol`, `decimals`, `is_core_reward` (`crv/cvx`), `first_seen_at`, `last_seen_at`
3. `strategy_reward_tokens`
   - `strategy_address`, `token_address`, `source` (`CORE|STRATEGY_REWARDS_TOKENS|MANUAL`), `active`, `first_seen_at`, `last_seen_at`
   - PK suggestion: (`strategy_address`, `token_address`)
4. `strategy_reward_snapshots`
   - `id`, `strategy_address`, `token_address`, `block_number`, `raw_balance`, `normalized_balance`, `timestamp`
5. `price_quotes`
   - `id`, `token_address`, `source`, `quote_usd`, `confidence`, `latency_ms`, `timestamp`, `request_id`
6. `auction_events`
   - `id`, `strategy_address`, `token_address`, `auction_address`, `event_type`, `tx_hash`, `metadata_json`, `timestamp`
7. `cowswap_orders`
   - `id`, `auction_event_id`, `order_uid`, `status`, `sell_amount`, `buy_amount`, `limit_price`, `expires_at`, `metadata_json`
8. `task_runs`
   - `id`, `task_type`, `status`, `started_at`, `finished_at`, `error_code`, `error_message`, `context_json`

Pragmatic schema note:

1. Prefer row-per-token snapshots (above) as canonical store for simple querying/sorting.
2. A JSON cache field on `strategies` (for last-known rewards token list) is fine, but should not be the only source of truth.
3. If you want maximum simplicity, add optional `snapshot_json` on `task_runs` for raw dump/debug without replacing normalized tables.
4. For SQLite, store addresses as lowercase `TEXT` consistently and enforce normalization at write-time.

### 7.2 Indexing

1. `(strategy_address, timestamp desc)` for latest balances.
2. `(token_address, timestamp desc, source)` for price history.
3. `(auction_address, event_type, timestamp)` for auction timelines.
4. partial index on `cowswap_orders(status in ('OPEN','PENDING'))`.

## 8. Strategy Discovery & Scanning

### 8.1 Discovery (Yearn Curve Factory)

Primary discovery path:

1. Call Yearn Curve factory at `0x21b1fc8a52f179757bf555346130bf27c0c2a17a`.
2. Read vault list with `allDeployedVautls()` (exact ABI signature must be verified).
3. For each vault, loop `withdrawalQueue(i)` from `i=0..N`.
4. Stop each vault loop when `withdrawalQueue(i) == address(0)`.
5. De-duplicate strategies globally and persist vault->strategy mapping.

Secondary/fallback modes:

1. `static`: strategies listed directly in config.
2. `manual override`: allow explicit include/exclude lists for emergency operations.

### 8.2 Reward Token Resolution

Because strategies vary, use adapter interface:

```python
class StrategyAdapter(Protocol):
    def list_reward_tokens(self, strategy_address: ChecksumAddress) -> list[ChecksumAddress]: ...
    def read_reward_balance(self, strategy_address: ChecksumAddress, token: ChecksumAddress) -> int: ...
```

Fallback behavior:

1. Core rewards are always included: `CRV` and `CVX`.
2. Union with any addresses returned by `strategy.rewardsTokens()`.
3. If `rewardsTokens()` is unavailable/reverts, keep core rewards and log adapter warning.
4. If token metadata unknown, resolve ERC20 `symbol/decimals` lazily and cache.

### 8.3 Scan Loop

Per cycle:

1. Resolve active strategies.
2. Resolve reward tokens per strategy (`CRV`, `CVX`, plus `rewardsTokens()`).
3. Read balances at a consistent block tag (`latest` by default; optional finalized block offset).
4. Persist snapshots.
5. Trigger pricing pipeline for non-zero balances or changed balances.

## 9. Pricing Engine (Plugin Architecture)

### 9.1 Provider Interface

```python
class PriceProvider(ABC):
    name: str
    priority: int
    async def quote_usd(self, chain_id: int, token: str, amount_wei: int) -> PriceQuoteResult: ...
    def supports(self, chain_id: int, token: str) -> bool: ...
```

### 9.2 Aggregation Policy

1. Gather quotes from enabled providers in priority order.
2. Accept first quote above confidence threshold for MVP.
3. Record all attempts (including failures/timeouts) for diagnostics.
4. Future: weighted median across sources.

### 9.3 Curve Provider (Initial)

Use endpoint pattern:

`https://www.curve.finance/api/router/v1/routes?chainId=1&tokenIn=<tokenIn>&tokenOut=<tokenOut>&amountIn=<amount>&router=curve`

For USD quote:

1. quote token -> USDC (or stable basket fallback).
2. normalize by token decimals and `amountIn`.
3. derive effective USD unit price.
4. return quote with source metadata (route hops, response timestamp, latency).

Safety rules:

1. reject stale/empty/unroutable responses.
2. reject extreme deviation vs recent known price (configurable guardrail).
3. if no valid quote, mark token as `UNPRICED` and skip automation.

## 10. Auction Automation

### 10.1 Yearn Auction Discovery

1. Read auction registry at `0x94F44706A61845a4f9e59c4Bc08cEA4503e48D12`.
2. Call `getLatestFactory()` for active factory address.
3. Use factory/auction ABIs to locate or create relevant auction instances for token/strategy pair.

Note: exact contract methods beyond `getLatestFactory()` must be validated against current Yearn auction ABIs and wired via typed wrapper classes.

### 10.2 Kick Policy

A strategy/token is eligible to kick when:

1. balance exceeds configured threshold (token or USD).
2. valid fresh price exists.
3. no conflicting active auction state for same inventory.
4. safety checks pass (gas ceiling, signer health, chain liveness).

Start price calculation:

`start_price = reference_price * (1 + start_price_buffer_bps / 10_000)`

Default buffer: `+10%`.

### 10.3 Idempotency

1. Acquire per-(strategy,token) lock before kick.
2. Re-check on-chain state immediately before sending tx.
3. Persist tx hash and resulting auction address/event.
4. On retries, detect already-started state and convert to success/no-op.

## 11. CoW Swap Automation

### 11.1 Responsibilities

1. Observe active auctions and current reference prices.
2. Determine when auction is near clearing price window.
3. Post/update CoW orders with configured slippage/expiry policy.
4. Track order UID lifecycle (`OPEN`, `FILLED`, `EXPIRED`, `CANCELLED`).

### 11.2 Posting Policy (MVP Opinionated)

1. Do not post if no valid fresh quote.
2. Post when auction discount is within configured band of market reference.
3. Use short expiries and periodic refresh to avoid stale resting orders.
4. One active order per auction leg unless explicit multi-order strategy enabled.

### 11.3 Failure Handling

1. HTTP/API failures: bounded retries + jitter.
2. Signature failures: hard-fail with high-priority alert.
3. Duplicate order detection by deterministic client-side idempotency key.

## 12. CLI Design

Use `Typer` with subcommands:

1. `tidal scan once`
2. `tidal scan daemon`
3. `tidal price quote --token <addr> --amount <wei>`
4. `tidal auction evaluate`
5. `tidal auction kick --strategy <addr> --token <addr> [--dry-run]`
6. `tidal cowswap sync`
7. `tidal run` (supervisor for all loops)
8. `tidal backfill prices --from <date> --to <date>`
9. `tidal healthcheck`

CLI output modes:

1. human-readable table (default interactive use)
2. JSON lines (`--json`) for automation pipelines

## 13. Observability & Logging

### 13.1 Structured Logging

Use `structlog` (JSON in prod, pretty console in dev).

Required fields on all logs:

1. `ts`, `level`, `service`, `env`, `chain_id`
2. `task`, `run_id`, `request_id`, `strategy`, `token`
3. `auction_address` or `order_uid` when applicable

### 13.2 Metrics

Expose Prometheus metrics endpoint:

1. counters:
   - `scan_cycles_total`
   - `auction_kick_attempts_total`
   - `auction_kick_success_total`
   - `cowswap_orders_posted_total`
   - `provider_errors_total{provider,error}`
2. histograms:
   - `rpc_call_latency_seconds`
   - `provider_quote_latency_seconds`
   - `auction_decision_latency_seconds`
3. gauges:
   - `last_successful_scan_timestamp`
   - `active_auctions`
   - `unpriced_tokens_count`

### 13.3 Alerting Signals

1. no successful scan for > N intervals
2. repeated quote failures for top reward tokens
3. repeated tx reverts on auction kick
4. high stale-order ratio on CoW

## 14. Reliability, Safety, and Controls

1. `dry_run` mode for all write actions.
2. global `kill_switch` config to disable tx/order submission without stopping reads.
3. per-token and global notional caps.
4. gas price ceiling and max priority fee constraints.
5. circuit breaker after consecutive critical failures.
6. explicit startup checks (RPC connectivity, chain ID, signer balance, DB migration status).

## 15. Testing Strategy (Strong Suite)

### 15.1 Unit Tests (`tests/unit`)

1. pricing math and normalization
2. buffer/start-price calculation
3. policy decisions (kick/no-kick, order/no-order)
4. idempotency key generation and state transition logic
5. config parsing/validation

### 15.2 Integration Tests (`tests/integration`)

1. mock Curve API responses (success, timeout, malformed, no-route)
2. mock CoW API lifecycle and order status polling
3. DB repository tests with transactional isolation
4. CLI command tests (Typer runner) for key flows

### 15.3 Mainnet-Fork Tests (`tests/fork`)

Use Anvil/Hardhat fork against Ethereum mainnet:

1. resolve latest factory via registry `getLatestFactory()`
2. read real strategy/token balances where possible
3. simulate kick transaction path in `dry_run` and gated live-call modes

### 15.4 End-to-End Tests (`tests/e2e`)

1. run full orchestration loop with local test config
2. verify persisted snapshots -> quotes -> auction decisions -> order intents
3. assert observability events emitted for each step

### 15.5 Quality Gates

1. `ruff check` + `ruff format --check`
2. `mypy` strict on core packages
3. test coverage target:
   - overall >= 85%
   - domain/policy modules >= 95%
4. CI matrix: Python 3.12/3.13, unit+integration always, fork tests on scheduled or manual runner

## 16. Dashboard-Ready Data Requirements (Deferred UI)

Store/query APIs must already support:

1. searchable strategy names and addresses
2. sortable rewards by token amount and USD value
3. “above threshold” flag with reason metadata
4. historical quote timeline per token/source
5. auction and order event timelines

This allows later dashboard implementation without changing core service contracts.

## 17. Delivery Plan (Phased)

### Phase 0: Foundation

1. repo scaffold, config, logging, DB migrations, CLI skeleton
2. web3 client wrapper + basic healthcheck

### Phase 1: Scanning + Curve Pricing

1. static strategy source
2. reward scanner + snapshot persistence
3. curve pricing provider + quote history

### Phase 2: Auction Kick Automation

1. registry/factory wrappers
2. kick policy + dry-run evaluator
3. guarded transaction sender + idempotency

### Phase 3: CoW Order Automation

1. order policy + builder
2. post/update/cancel lifecycle
3. order persistence + metrics/alerts

### Phase 4: Dashboard Integration Prep

1. query endpoints/read models
2. report/export commands for UI or BI ingestion

## 18. Open Questions for Iteration

1. Strategy discovery source of truth: static config only, or pull from on-chain Yearn registries from day one?
2. Exact Yearn auction ABI/version support matrix for current mainnet deployments.
3. CoW posting strategy details: single order vs laddered orders.
4. Required SLA for scan-to-kick latency.
5. Operational preference: single process multi-loop vs separate worker processes.

## 19. External References

1. Yearn Auctions docs: https://docs.yearn.fi/developers/auctions
2. Auction registry (mainnet): `0x94F44706A61845a4f9e59c4Bc08cEA4503e48D12`
3. Curve pricing route format:
   `https://www.curve.finance/api/router/v1/routes?chainId=1&tokenIn=0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee&tokenOut=0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48&amountIn=1000000000000000000&router=curve`

## 20. Definition of Done (for initial implementation)

1. A fresh environment can run `tidal run --dry-run` and complete scan->price->decision loops without errors.
2. At least one strategy/token can be scanned, priced (Curve), and evaluated for kick deterministically.
3. Auction kick path is fully implemented with explicit safety gates and idempotency.
4. CoW order lifecycle can be simulated end-to-end in integration tests.
5. Observability includes structured logs, metrics endpoint, and failure alerts.
6. CI passes lint, typing, and required test suites.
