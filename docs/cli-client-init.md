# `tidal init`

Scaffold the one shared configuration and secret-file example:

```bash
tidal init --dest config
```

Existing files are kept unless `--force` is explicit. This command does not
create a database, key, remote client or activation. Fill the shared policy and
select a private `TIDAL_ENV_FILE`; see [configuration](config.md).

Use `tidal db init` only for deliberately empty state. Restore or explicitly
migrate existing state using [the recovery procedure](recovery.md).
