# CLI Client: `tidal init`

`tidal init` creates the default workstation home for the CLI client.

## Common Invocation

```bash
tidal init
```

Overwrite the scaffolded files intentionally:

```bash
tidal init --force
```

## What It Writes

The command creates:

- `~/.tidal/config.yaml`
- `~/.tidal/.env`
- `~/.tidal/state/`
- `~/.tidal/state/operator/`
- `~/.tidal/run/`

## When To Use It

Run `tidal init` when:

- setting up a new workstation
- you want to regenerate the latest scaffold files with `--force`

Use `tidal-server init-config` for tracked server config in the repo.

## What To Edit Next

After initialization, the usual next steps for a CLI client are:

1. Put `TIDAL_API_KEY` in `~/.tidal/.env`.
2. If you are using `https://api.tidal.wavey.info`, get that API key from wavey.
3. Confirm `tidal_api_base_url` in `~/.tidal/config.yaml`.
4. Add keystore-related values if you will broadcast locally.

See [Configuration](config.md) for the setting-level breakdown.
