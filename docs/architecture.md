# Architecture

One runtime owns observation, preparation, managed execution and recovery. One
SQLite database stores discovered state, policy history, business operations,
notification delivery state and a single transaction ledger. One YAML policy
and one secret file supply every service and local command.

The API opens read-only database connections. Dashboard reads and unsigned
previews create no action job, outbox or receipt task. The API constructs no
signer, broadcaster or background reconciliation loop. Browser-wallet
deployment remains a separate, stateless wallet flow.

## Managed execution

Local scanner, kick and auction commands share a stable host lock outside the
database directory. Preparation rechecks current state, policy and prices.
The common sender verifies activation, signer, chain freshness, expected nonce
and quote age. It signs in memory, then atomically commits the exact hash,
signer, nonce, unsigned intent and every business-operation link before one RPC
broadcast. Signed bytes are not persisted.

One unresolved attempt blocks the signer, including an included transaction
that has not finalized. Reconciliation fetches fresh transaction, receipt,
canonical block and finalized-head evidence. Business changes are committed
atomically only when intent and required events match. Missing or conflicting
evidence remains visible for review. There is no automatic rebroadcast,
replacement or nonce-based guess about an unknown historical transaction.

## Recovery boundary

Tidal owns DB validation, legacy migration, transaction proof, current-state
refresh, notification baselines, scoped policy repair and activation. The backup
system owns snapshot storage, credentials, artifact retention, file replacement,
service fencing and the restore journal. It calls native commands rather than
querying Tidal tables or importing its internal classes.

Recovery refresh constructs observation-only services with no signer, price
client or notification transport. Historical amounts, timestamps, round links,
no-fill delays and reviewed baselines survive. Current quotes can wait for a
provider while the API serves restored data. See [recovery](recovery.md) for the
concrete operator sequence and acceptance evidence.
