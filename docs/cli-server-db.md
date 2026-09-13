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

Follow [backup and recovery](recovery.md) for the complete sequence. The
`tidal-server db` entry point addresses the same commands and same state.
