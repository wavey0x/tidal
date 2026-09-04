# API Reference

This page is derived from the FastAPI app assembly in `tidal/api/app.py`, the route files in `tidal/api/routes/`, and the request schemas in `tidal/api/schemas/`.

Base prefix:

```text
/api/v1/tidal
```

Health endpoint:

```text
/health
```

## Response Envelope

Most endpoints return:

```json
{
  "status": "ok",
  "warnings": [],
  "data": {}
}
```

Possible `status` values include:

- `ok`
- `noop`
- `error`

## Authentication

Authenticated endpoints require:

```text
Authorization: Bearer <api-key>
```

Auth behavior:

- missing/invalid bearer token: `401`
- no API keys configured: `503`
- operator identity is currently the API key label

## Public Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness/ready check |
| `GET` | `/api/v1/tidal/dashboard` | Dashboard payload for strategies, fee burners, balances, and auction metadata |
| `GET` | `/api/v1/tidal/alerts` | Current read-only operational alerts and scheduled retry watches |
| `GET` | `/api/v1/tidal/logs/kicks` | Kick history with filters |
| `GET` | `/api/v1/tidal/logs/scans` | Scan run history |
| `GET` | `/api/v1/tidal/logs/runs/{run_id}` | Detail for one historical run |
| `POST` | `/api/v1/tidal/kick/inspect` | Candidate inspection using cached shortlist data plus optional live inspection |
| `GET` | `/api/v1/tidal/kicks/{kick_id}/auctionscan` | Resolve AuctionScan auction/round links for a kick row |
| `GET` | `/api/v1/tidal/strategies/{strategy}/deploy-defaults` | Load deploy defaults for a strategy |
| `POST` | `/api/v1/tidal/auctions/deploy/browser-prepare` | Prepare an unsigned wallet deployment without an operator action record |

## Authenticated Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/tidal/kick/prepare` | Prepare one or more kick actions |
| `POST` | `/api/v1/tidal/auctions/deploy/prepare` | Prepare an auction deployment |
| `POST` | `/api/v1/tidal/auctions/{auction}/enable-tokens/prepare` | Prepare enable-token calls |
| `POST` | `/api/v1/tidal/auctions/{auction}/settle/prepare` | Prepare an auction settlement |
| `GET` | `/api/v1/tidal/actions` | List prepared actions and audit state |
| `GET` | `/api/v1/tidal/actions/{action_id}` | Fetch one action |
| `POST` | `/api/v1/tidal/actions/{action_id}/broadcast` | Report a locally broadcast transaction |
| `POST` | `/api/v1/tidal/actions/{action_id}/receipt` | Request chain receipt reconciliation |

## Query Parameters

### `GET /logs/kicks`

- `limit` default `100`, max `500`
- `offset` default `0`
- `status`
- `source`
- `auction`

### `GET /logs/scans`

- `limit` default `100`, max `500`
- `offset` default `0`
- `status`

### `GET /actions`

- `limit` default `100`, max `500`
- `offset` default `0`
- `operator`
- `status`
- `action_type`

## Request Bodies

### `POST /kick/inspect`

```json
{
  "sourceType": "strategy",
  "sourceAddress": "0x...",
  "auctionAddress": "0x...",
  "tokenAddress": "0x...",
  "limit": 5,
  "minUsdValue": 200,
  "includeLiveInspection": true
}
```

### `POST /kick/prepare`

Adds:

```json
{
  "sender": "0x...",
  "requireCurveQuote": true,
  "minUsdValue": 200,
  "allowNoFillRetry": false
}
```

`allowNoFillRetry` requires an exact `auctionAddress` and `tokenAddress` and
bypasses only an exhausted no-fill retry budget for that one preparation.

### `POST /auctions/deploy/prepare`

```json
{
  "want": "0x...",
  "receiver": "0x...",
  "sender": "0x...",
  "factory": "0x...",
  "governance": "0x...",
  "startingPrice": 1234,
  "salt": "0x..."
}
```

### `POST /auctions/{auction}/enable-tokens/prepare`

```json
{
  "sender": "0x...",
  "extraTokens": ["0x..."]
}
```

### `POST /auctions/{auction}/settle/prepare`

```json
{
  "sender": "0x...",
  "tokenAddress": "0x...",
  "method": "auto"
}
```

### `POST /actions/{action_id}/broadcast`

```json
{
  "sender": "0x...",
  "txHash": "0x...",
  "broadcastAt": "2026-03-29T17:00:10.750381+00:00",
  "txIndex": 0
}
```

### `POST /actions/{action_id}/receipt`

```json
{
  "txIndex": 0
}
```

This is a reconciliation hint, not a client assertion of success or failure.
Legacy receipt fields are accepted but ignored. The server looks up the retained
broadcast hash and checks its chain, prepared sender (when specified), destination,
calldata and value before committing chain-derived action and operation outcomes.
`receiptStatus` and mined details remain null until `verifiedAt` is populated.
Lookup failures leave the action pending and return a warning; broadcast replay
does not downgrade verified outcomes.

With server RPC configured, one API background task retries up to 20 pending
transactions per pass, using `TIDAL_API_RECEIPT_RECONCILE_INTERVAL_SECONDS` and
`TIDAL_API_RECEIPT_RECONCILE_THRESHOLD_SECONDS`. This also covers API-only deployment
actions. Migration 0027 requeues retained historical hashes for verification while
preserving reported fields in the database for audit. It does not send transactions.

## Errors

Structured application errors return:

```json
{
  "status": "error",
  "warnings": [],
  "data": null,
  "detail": "..."
}
```

Important error cases:

- `401`: bearer token required or invalid
- `404`: missing run/action
- `500`: generic database operation failure
- `503`: SQLite lock contention or no API keys configured

The API explicitly maps SQLite lock errors to:

```text
database is locked; retry the request
```

## Operational Notes

- Dashboard, Alerts, and logs are intentionally public.
- Mutating routes are authenticated.
- The server prepares actions, but the CLI client signs locally.
- The scanner runs one bounded shared receipt-reconciliation pass before evaluating operational alerts.
