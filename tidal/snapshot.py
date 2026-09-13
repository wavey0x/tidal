"""Publish a coherent, verified SQLite snapshot with its native schema identity."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from urllib.parse import quote

from tidal.lifecycle import LifecycleError, inspect_database, result
from tidal.time import utcnow_iso


def snapshot(source: Path, output: Path, *, timeout_seconds: int = 300) -> dict:
    source, output = source.expanduser().resolve(), output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise LifecycleError("OUTPUT_EXISTS", "Snapshot output must be a new file; preserve existing recovery points.")
    inspect_database(source)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tidal-snapshot-", suffix=".sqlite3", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    started = time.monotonic()

    def progress(_status, _remaining, _total):
        if time.monotonic() - started > timeout_seconds:
            raise LifecycleError("BUSY", "Snapshot did not complete within its deadline; retry later.")

    try:
        uri = f"file:{quote(str(source), safe='/')}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as origin, closing(sqlite3.connect(temporary)) as target:
            origin.execute("PRAGMA query_only=ON")
            origin.backup(target, pages=256, progress=progress, sleep=0.05)
            target.execute("PRAGMA journal_mode=DELETE")
        info = inspect_database(temporary)
        with temporary.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            os.fsync(stream.fileno())
        # Hard-link publication refuses an output created concurrently, and
        # never replaces another backup or a live DB selected by mistake.
        os.link(temporary, output)
        descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return result("SNAPSHOT_CREATED", data={**info, "path": str(output), "sha256": checksum,
            "bytes": output.stat().st_size, "captured_at": utcnow_iso()})
    except FileExistsError as exc:
        raise LifecycleError("OUTPUT_EXISTS", "Snapshot output appeared concurrently; preserve it and choose a new path.") from exc
    except (sqlite3.Error, OSError) as exc:
        raise LifecycleError("SNAPSHOT_FAILED", f"Snapshot could not finish ({type(exc).__name__}); inspect available space and retry.") from exc
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
