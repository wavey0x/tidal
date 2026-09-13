# `tidal api`

```bash
tidal api serve --config /path/to/server.yaml
```

Serve the read-only API from an existing compatible database. Default binding is
`0.0.0.0:8787`; production should select its declared loopback listener and reverse
proxy. `TIDAL_API_HOST` and `TIDAL_API_PORT` override those defaults.

Startup neither creates nor migrates state. It constructs no signer,
broadcaster or background receipt worker. Unsigned previews remain stateless.
`/health` checks actual schema/identity; chain, price and execution readiness are
reported separately by `tidal status`.

The restored API can start while worker activation and dependencies are held.
See [API reference](api-reference.md) and [recovery](recovery.md).
