# `tidal db`

Database lifecycle is explicit. Services do not initialize or migrate at startup.

```bash
tidal db check --database /path/to/tidal.db --json
tidal db snapshot --database /path/to/tidal.db --output /backup/new.sqlite3 --json
```

`db init` deliberately creates empty held state and refuses an existing file.
`db migrate` upgrades while held. The first consolidation migration requires
separate protected original `--source-database` and `--outbox` files so useful
submission evidence is imported before old action tables are retired.

`db prepare-restore --credential-file FILE --json` rotates API access, marks
prices stale and prepares notification suppression while preserving history.
`db repair-auction-rounds` requires an exact `--auction` and `--token`; preview
before `--apply`. It refuses unresolved attempts and does not baseline other
pairs. `db clear-no-fill-suspension` is a separate explicit policy override for
a selected pair, with a read-only preview before `--apply`.

Settlement discovery searches each unresolved round from its kick to the next
confirmed kick for that auction/token, or the finalized head. It stops at a
verified close and permits at most 20 chunks of 50,000 blocks per round. Completed
rounds and complete receipt evidence do not consume the RPC lookup allowance.
Pending attempts still belong to the transaction ledger. Missing business-event
fields in confirmed records are repaired through that same ledger; missing native
identity metadata alone does not replay complete legacy business history.

If the search allowance is exhausted, the warning identifies the auction, token
and kick. That round stays unresolved, and other rounds continue. To verify a
known direct settlement beyond the allowance, use the exact transaction:

```bash
tidal db repair-auction-rounds --auction 0xAUCTION --token 0xTOKEN --kick-id 123 --settlement-tx 0xTRANSACTION --apply --json
```

Both selectors are required. The transaction must contain a successful finalized
settlement for that pair, strictly after the selected kick and before its next
kick in block/transaction order. This path uses the same receipt verification and
duplicate checks as discovery and never creates historical baselines. Managed
resolution transactions, which also record recovered amounts, remain the ledger's
responsibility. Exact settlement evidence can be recorded while unrelated ledger
attempts remain pending; those attempts retain their existing execution blockers.
Neither repair path broadcasts a transaction.

Follow [backup and recovery](recovery.md) for the complete sequence. The
`tidal-server db` entry point addresses the same commands and same state.
