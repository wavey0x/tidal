# Server Operator: `tidal-server api`

`tidal-server api` serves the FastAPI control plane used by the dashboard and the CLI client.

## Subcommands

- `serve`

## Common Invocation

```bash
tidal-server api serve --config config/server.yaml
```

## Runtime Behavior

The API process binds to `0.0.0.0:8787` by default.
Override with environment variables or explicit `config/server.yaml` keys only when you need non-default wiring:

- `tidal_api_host`
- `tidal_api_port`

In production, it is normally placed behind a reverse proxy or TLS terminator.

## Operational Notes

- Run `tidal-server db migrate` before starting the API.
- If `RPC_URL` is present, the API can run its background receipt reconciliation loop for action audit rows that already have a known transaction hash.
- The API is the control plane for `tidal`, not the holder of private keys. Signing stays on the CLI client or the server operator host that explicitly broadcasts.
