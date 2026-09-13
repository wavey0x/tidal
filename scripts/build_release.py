#!/usr/bin/env python3
"""Build one recoverable Linux Tidal release from an exact committed revision.

Only building uses package registries. The resulting artifact contains source,
built UI, pinned wheels and the maintained Python/SQLite runtime for offline
installation. Mutable application state and activation never enter the bundle.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile

from prepare_release import digest


def run(args, cwd, *, extra=None, capture=False):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "PYTHONDONTWRITEBYTECODE": "1",
           "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_CONFIG_FILE": os.devnull, **(extra or {})}
    return subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True,
        stdout=subprocess.PIPE if capture else None).stdout


def build(repository, revision, runtime, output, api_base_url):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("Build and rehearse this release on Linux x86_64.")
    if not re.fullmatch("[a-f0-9]{40}", revision):
        raise ValueError("Select a complete immutable commit ID.")
    repository, runtime, output = (path.expanduser().resolve() for path in (repository, runtime, output))
    source = run(["git", "archive", "--format=tar", revision], repository, capture=True)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tidal-build-", dir=output) as temporary:
        temporary = Path(temporary)
        root = temporary / "release"
        root.mkdir()
        with tarfile.open(fileobj=io.BytesIO(source)) as archive:
            archive.extractall(root, filter="data")
        for name in (".python", ".venv", "wheelhouse", "requirements.runtime.txt", "electro-release.json", ".prepared"):
            if (root / name).exists():
                raise ValueError("Source uses reserved release member: " + name)
        shutil.copytree(runtime, root / ".python", symlinks=False,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        python = root / ".python/bin/python3.12"
        versions = json.loads(run([python, "-I", "-c",
            "import json,sys,sqlite3; print(json.dumps({'python':sys.version.split()[0], 'sqlite':sqlite3.sqlite_version}))"], root, capture=True))
        if versions != {"python": "3.12.14", "sqlite": "3.53.1"}:
            raise ValueError("Use the rehearsed Python 3.12.14 / SQLite 3.53.1 runtime.")
        run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements-txt",
             "--output-file", "requirements.runtime.txt"], root)
        wheels = root / "wheelhouse"
        wheels.mkdir()
        run([python, "-m", "venv", temporary / "build-venv"], root)
        build_python = temporary / "build-venv/bin/python"
        run([build_python, "-m", "pip", "download", "--only-binary=:all:", "--require-hashes",
             "--dest", wheels, "-r", root / "requirements.runtime.txt"], root)
        run(["uv", "build", "--python", python, "--wheel", "--out-dir", wheels], root)
        application = list(wheels.glob("tidal-*.whl"))
        if len(application) != 1:
            raise ValueError("Expected exactly one application wheel.")
        run(["npm", "ci", "--no-audit", "--no-fund"], root / "ui")
        run(["npm", "run", "build"], root / "ui", extra={"VITE_TIDAL_API_BASE_URL": api_base_url})
        shutil.rmtree(root / "ui/node_modules")
        # A wheel build may leave local build metadata. Keep only useful source
        # and the explicit runtime members, not cache or build environments.
        for cache in root.rglob("__pycache__"):
            shutil.rmtree(cache)
        files = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Release source contains an indirect member: " + str(path.relative_to(root)))
            if path.is_file():
                files[str(path.relative_to(root))] = digest(path)
        metadata = {"format": "tidal-runtime-v1", "revision": revision,
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "requirements_sha256": digest(root / "requirements.runtime.txt"),
            "runtime": {**versions, "application": "0.1.0"},
            "application_wheel": str(application[0].relative_to(root)),
            "ui_api_base_url": api_base_url, "files": files}
        (root / "electro-release.json").write_text(json.dumps(metadata, sort_keys=True) + "\n")
        staged = temporary / "release.tar.gz"
        with tarfile.open(staged, "w:gz") as archive:
            for path in sorted(root.iterdir()):
                archive.add(path, arcname=path.name)
        checksum = digest(staged)
        target = output / (checksum + ".tar.gz")
        staged.replace(target)
        return {key: value for key, value in metadata.items() if key != "files"} | {
            "archive": str(target), "sha256": checksum}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-base-url", default="https://api.tidal.wavey.info/api/v1/tidal")
    args = parser.parse_args()
    print(json.dumps(build(args.repository, args.revision, args.runtime, args.output, args.api_base_url), sort_keys=True))
