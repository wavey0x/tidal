#!/usr/bin/env python3
"""Verify a retained Tidal artifact and install its wheels without the network.

Run from the final immutable release directory. No config, DB, credential or
service is touched. A failed preparation can be rerun at the same location.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(root):
    root = Path(root).resolve()
    metadata = json.loads((root / "electro-release.json").read_text())
    if metadata.get("format") != "tidal-runtime-v1" or platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("This artifact requires Linux x86_64 and the Tidal runtime format.")
    for name, expected in metadata["files"].items():
        path = root / name
        if not path.is_relative_to(root) or path.is_symlink() or path.resolve() != path.absolute() or not path.is_file():
            raise ValueError("Release contains a missing or indirect member: " + name)
        if digest(path) != expected:
            raise ValueError("Release member checksum changed: " + name)
    return metadata


def prepare(root):
    root = Path(root).resolve()
    metadata = verify(root)
    marker = root / ".prepared"
    if (root / ".venv").is_symlink():
        raise ValueError("Refusing an indirect runtime directory.")
    fingerprint = digest(root / "electro-release.json")
    if marker.exists():
        if marker.is_symlink() or marker.read_text().strip() != fingerprint:
            raise ValueError("Prepared release identity differs from the retained artifact.")
    else:
        # Only this installer creates this directory; a completed release is
        # never rebuilt. An incomplete installation has no authority to run.
        venv = root / ".venv"
        if venv.exists():
            shutil.rmtree(venv)
        run([str(root / ".python/bin/python3.12"), "-m", "venv", str(venv)], root)
        python = str(venv / "bin/python")
        run([python, "-m", "pip", "install", "--no-index", "--require-hashes",
             "--find-links", str(root / "wheelhouse"), "-r", str(root / "requirements.runtime.txt")], root)
        run([python, "-m", "pip", "install", "--no-index", "--no-deps",
             str(root / metadata["application_wheel"])], root)
    python = str(root / ".venv/bin/python")
    run([python, "-m", "pip", "check"], root)
    actual = json.loads(run([python, "-I", "-c",
        "import json,sys,sqlite3,tidal; print(json.dumps({'python':sys.version.split()[0], 'sqlite':sqlite3.sqlite_version, 'application':tidal.__version__}))"], root, capture=True))
    if actual != metadata["runtime"]:
        raise ValueError("Installed runtime differs from the saved Python/SQLite/application versions.")
    run([python, "-I", "-m", "tidal.cli", "--help"], root, capture=True)
    if not marker.exists():
        temporary = root / ".prepared.tmp"
        with temporary.open("w") as stream:
            stream.write(fingerprint + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(marker)
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {"status": "prepared", "release_path": str(root), "revision": metadata["revision"],
            "manifest_sha256": fingerprint, "runtime": actual}


def run(args, cwd, *, capture=False):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd),
           "PYTHONDONTWRITEBYTECODE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
           "PIP_NO_CACHE_DIR": "1", "PIP_CONFIG_FILE": os.devnull}
    return subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
        stdout=subprocess.PIPE if capture else None).stdout


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.release) if args.verify_only else prepare(args.release), sort_keys=True))
