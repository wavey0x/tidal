# `tidal auction`

Native auction commands prepare and execute under the common local lock:

```bash
tidal auction enable-tokens 0xAUCTION
tidal auction enable-tokens 0xAUCTION --extra-token 0xTOKEN
tidal auction settle 0xAUCTION --token 0xTOKEN
tidal auction sweep 0xAUCTION --token 0xTOKEN
```

Interactive commands show a fresh preview and request confirmation. Unattended
JSON requires `--no-confirmation`. Keystore overrides must still match the
declared and activated signer. Use `--help` for command-specific force options.

Every send retains identity, unsigned intent and all operation links before
broadcasting. A pending first transaction stops subsequent sends; the result
retains its hash and reports remaining work. Reconcile that attempt rather than
resubmitting a prepared payload.

New auction deployment uses the browser wallet flow. The managed CLI has no
deployment command. The read-only API's previews create no durable action job.
