# Local development

Use the committed dependency lock and a separate development database:

```bash
uv sync --frozen --extra dev
uv run tidal init
uv run tidal db init --config config/server.yaml
uv run tidal api serve --config config/server.yaml
```

Select `TIDAL_HOME`, `DB_PATH` and a private `TIDAL_ENV_FILE` explicitly when
working alongside production settings. All commands share one configuration;
there is no CLI client home or remote operator API mode. See [configuration](config.md).

For fixture-based tests and docs:

```bash
uv run pytest tests/unit tests/integration
uv run mkdocs build --strict
```

Unit/integration tests prohibit live network access. Keep real signing keys and
delivery credentials out of fixtures. Contract/fork tests in `contracts/` and
`tests/fork/` are separate and need their explicitly configured fork environment.

For the UI:

```bash
cd ui
npm ci
TIDAL_API_PROXY_TARGET=http://127.0.0.1:8787 npm run dev
npm test
npm run build
npm run test:browser
```

The UI needs only public API URL configuration. Its browser fixtures do not send
production transactions. Follow the repository's light/dark theme, contrast and
copy-affordance checks before shipping UI changes.

Production [release preparation](install.md) packages the maintained runtime,
locked wheels and built UI. Rehearse installation and recovery with networking
disabled and fixture identities before changing live service paths.
