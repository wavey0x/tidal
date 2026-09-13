# API reference

The API opens read-only database connections and serves dashboard data, logs,
transaction evidence and stateless unsigned previews. It never records a
prepared job, signs, broadcasts, accepts receipt reports or reconciles in the
background. Local managed execution uses the native CLI.

Routes use `/api/v1/tidal`. Responses contain `status`, `warnings` and `data`.
Request schemas and exact query constraints are available from `/openapi.json`
or the running API's `/docs` page.

| Method and path | Access | Purpose |
|---|---|---|
| `GET /dashboard` | Public | Cached strategies and fee burners |
| `GET /alerts` | Public | Retained alert view |
| `GET /logs/kicks` | Public | Business operations and outcomes |
| `GET /logs/scans` | Public | Scanner history |
| `GET /logs/runs/{run_id}` | Public | Detailed retained run |
| `GET /kicks/{kick_id}/auctionscan` | Public | Optional auction enrichment |
| `POST /kick/inspect` | Public | Candidate inspection |
| `GET /strategies/{strategy}/deploy-defaults` | Public | Browser deployment defaults |
| `POST /auctions/deploy/browser-prepare` | Public | Unsigned wallet deployment preview |
| `GET /transactions` | Bearer key | Single managed transaction ledger |
| `GET /transactions/{transaction_id}` | Bearer key | Identity and linked business operations |
| `POST /kick/prepare` | Bearer key | Stateless unsigned kick preview |
| `POST /auctions/deploy/prepare` | Bearer key | Stateless unsigned deployment preview |
| `POST /auctions/{auction}/enable-tokens/prepare` | Bearer key | Stateless enablement preview |
| `POST /auctions/{auction}/settle/prepare` | Bearer key | Stateless settlement preview |
| `POST /auctions/{auction}/sweep/prepare` | Bearer key | Stateless sweep preview |

`/transactions` supports `limit`, `offset`, `status` and `profile`. Individual
transactions expose unsigned intent and business links; signed payloads are
never retained. Pending/included states are provisional. Confirmation requires
canonical finalized evidence and matching business events.

API keys are created, listed and revoked locally with `tidal auth`. Protected
routes require `Authorization: Bearer ...`. Restore preparation revokes old
keys and writes fresh access to a private file. Browser dashboard use does not
require an operator key.

`GET /health` sits outside the API prefix. It checks the actual database schema
and identity and returns 503 for unavailable/incompatible state. It is separate
from chain, price and execution readiness reported by `tidal status`.

Old `/actions`, broadcast-report and receipt-report endpoints are removed.
Unsigned previews contain no durable action ID and do not reserve a nonce or
grant permission to send. A managed command always prepares again from current
conditions under its local execution lock.
