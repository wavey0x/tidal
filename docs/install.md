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

## Electro deployment entry point

The application-owned `deploy-tidal.sh` accepts an explicitly selected archive.
It never fetches a moving branch or installs the server-backup infrastructure.
Prepare the original encrypted key and effective legacy configuration with the
new release's interpreter before the first cutover:

```bash
RELEASE/.venv/bin/python RELEASE/scripts/prepare_legacy_config.py --legacy-python /home/wavey/tidal/venv/bin/python --output /home/wavey/tidal-cutover-inputs
sudo bash RELEASE/deploy-tidal.sh install --archive /home/wavey/tidal-artifacts/ARCHIVE_SHA256.tar.gz --sha256 ARCHIVE_SHA256 --configuration /home/wavey/tidal-cutover-inputs
```

Installation persists service holds, stops old writers and deployer aliases,
retains the coherent original DB/outbox and split configuration/runtimes, and
verifies their archive on the existing Storage Box before migration. It installs
one root-owned runtime and private service-user configuration. Existing console
entry points select that runtime. Retired service aliases stay masked.

The operation journal is rooted at `/var/lib/electro/apps/tidal`. Repeat the
same command and candidate after interruption; a retry never recopies old state
over the migration target. The result starts only the read-only API. Execution
remains held, including after a reboot.

The installer adds one native capture call to the existing daily file-mirror
script. It preserves that script's other jobs and schedule, and excludes Tidal's
live database/activation and disposable runtime copies from the home mirror.
The capture retains the database, exact offline release, configuration, encrypted
key, units and checksums together in `/mnt/storage-box/backup/tidal-native`.
There is no new backup engine, timer or secondary destination requirement.

Before enabling execution, check the public API/UI and retrieve a capture:

```bash
sudo /usr/local/sbin/tidal-backup
sudo bash /home/wavey/tidal/deploy-tidal.sh resume
```

Resume checks the capture's release and database identity, invokes native
reconciliation, silent recovery refresh and activation, then runs ordinary scan
and kick cycles before enabling their timers. Failure leaves workers held.
Verify a post-cutover capture and the next existing daily scheduled capture.
The shared outer lock is `/var/lib/electro-backup/locks/application-tidal`;
creating this lock does not deploy the later infrastructure service.
