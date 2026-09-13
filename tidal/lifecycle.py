"""Application-owned database checks, execution coordination and activation.

No RPC, pricing, signing or notification clients are constructed here. Service
orchestration must stop every DB user before replacing files or migrating.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sqlite3
import tempfile
import threading
import uuid
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Mapping

from tidal.normalizers import normalize_address
from tidal.errors import AddressNormalizationError
from tidal.time import utcnow_iso

INTERFACE_VERSION = 1
SCHEMA_REVISION = "0031_retire_action_protocol"
_locks: ContextVar[dict[Path, tuple[object, ...]]] = ContextVar("tidal_execution_locks", default={})


class LifecycleError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def result(code: str, *, data: dict | None = None, blockers: list | None = None, warnings: list | None = None) -> dict:
    return {
        "interface_version": INTERFACE_VERSION,
        "code": code,
        "blockers": blockers or [],
        "warnings": warnings or [],
        "data": data or {},
    }


def _owner() -> tuple[object, ...]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return os.getpid(), threading.get_ident(), task


@contextmanager
def execution_lock(path: Path) -> Iterator[None]:
    """One stable, nonblocking lock; nesting is allowed only for the same caller.

    Never unlink the lock: replacing its inode can admit a second writer. A
    copied async context or forked child must still acquire its own kernel lock.
    """
    path = path.expanduser().absolute()
    owner = _owner()
    if _locks.get().get(path) == owner:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    token = None
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise LifecycleError("BUSY", "Another Tidal operation holds the execution lock; retry later.") from exc
        token = _locks.set({**_locks.get(), path: owner})
        yield
    finally:
        if token is not None:
            _locks.reset(token)
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inspect_database(path: Path) -> dict:
    """Inspect existing state without opening dependencies or creating a DB."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise LifecycleError("MISSING_DB", "Database is missing; explicitly restore or initialize it.")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise LifecycleError("INVALID_DB", "Database integrity check failed.")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise LifecycleError("INVALID_DB", "Database contains broken foreign keys.")
            revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
            if revisions != [(SCHEMA_REVISION,)]:
                raise LifecycleError("INCOMPATIBLE_SCHEMA", "Install the matching release or explicitly migrate while held.")
            from tidal.persistence.models import metadata

            for table in metadata.sorted_tables:
                actual = {row[1] for row in connection.execute(f'PRAGMA table_info("{table.name}")')}
                if not {column.name for column in table.columns} <= actual:
                    raise LifecycleError("INCOMPATIBLE_SCHEMA", f"Required columns are missing from {table.name}.")
            rows = connection.execute(
                "SELECT id, database_identity, notification_baseline_pending, recovery_refreshed_at, "
                "recovery_block_number, recovery_block_hash FROM app_metadata"
            ).fetchall()
            if len(rows) != 1 or rows[0][0] != 1 or rows[0][2] not in (0, 1):
                raise LifecycleError("INVALID_METADATA", "Database recovery metadata is invalid.")
            identity = str(uuid.UUID(rows[0][1]))
            return {
                "database_identity": identity,
                "schema_revision": SCHEMA_REVISION,
                "notification_baseline_pending": bool(rows[0][2]),
                "recovery_refreshed_at": rows[0][3],
                "recovery_block_number": rows[0][4],
                "recovery_block_hash": rows[0][5],
                "sqlite_version": sqlite3.sqlite_version,
            }
    except LifecycleError:
        raise
    except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
        raise LifecycleError("INVALID_DB", "Database schema or mandatory metadata cannot be read.") from exc


def activation_binding(database_identity: str, chain_id: int, signers: Mapping[str, str]) -> dict:
    if chain_id <= 0 or not signers or any(not str(profile).strip() for profile in signers):
        raise LifecycleError("WRONG_SIGNER", "Declare every managed signer profile before activation.")
    try:
        normalized = {profile: normalize_address(address) for profile, address in sorted(signers.items())}
    except (AddressNormalizationError, ValueError, TypeError) as exc:
        raise LifecycleError("WRONG_SIGNER", "A managed signer address is invalid.") from exc
    return {
        "interface_version": INTERFACE_VERSION,
        "database_identity": str(uuid.UUID(database_identity)),
        "chain_id": chain_id,
        "signers": normalized,
    }


def require_activation(path: Path, binding: dict) -> dict:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LifecycleError("HELD", "Execution is held; check readiness and explicitly resume.") from exc
    if not isinstance(current, dict) or any(current.get(key) != value for key, value in binding.items()):
        raise LifecycleError("HELD", "Activation does not match this database, chain and signer mapping.")
    return current


def clear_activation(path: Path) -> None:
    """Caller holds the execution lock. Also used before every supported restore."""
    if path.exists():
        path.unlink()
        _sync_directory(path.parent)


def write_activation(path: Path, binding: dict) -> None:
    """Caller holds the execution lock and has rechecked native readiness."""
    atomic_json(path, {**binding, "activated_at": utcnow_iso()})
