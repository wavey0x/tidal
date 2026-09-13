# Installation

Production uses one immutable Linux x86_64 release containing Python 3.12.14,
SQLite 3.53.1, locked dependency wheels, application source and a built UI. Build
from an exact committed revision on Linux:

```bash
python3 scripts/build_release.py --repository /path/to/tidal --revision FULL_COMMIT --runtime /path/to/maintained-python --output /path/to/artifacts
```

Verify the archive checksum, extract it into its final release directory, then:

```bash
/path/to/release/.python/bin/python3.12 /path/to/release/scripts/prepare_release.py /path/to/release
```

Preparation installs from retained wheels without package-registry access,
verifies dependencies and runtime versions, and creates no database or services.
Use the same `/path/to/release/.venv/bin/tidal` for API, scans, kicks and recovery.

Select one absolute `--config` path and one `TIDAL_ENV_FILE`; see
[configuration](config.md). Preserve the original encrypted signing identity.

For intentionally empty state, run `tidal db init`. For existing state, use
the [legacy transition or restore procedure](recovery.md). Never add migration
or initialization to service startup. Starting the read-only API does not
authorize managed execution; activation requires explicit `tidal resume`.

For source development, use `uv sync --frozen --extra dev` and
`uv run tidal ...`. Production recovery uses its saved artifact rather
than resolving a moving Git branch or fetching dependencies again.
