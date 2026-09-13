"""Artifact integrity checks run before an installer can execute retained code."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("tidal_prepare_release", Path(__file__).resolve().parents[2] / "scripts/prepare_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(release.platform, "system", lambda: "Linux")
    monkeypatch.setattr(release.platform, "machine", lambda: "x86_64")
    (tmp_path / "member").write_bytes(b"retained runtime")
    (tmp_path / "electro-release.json").write_text(json.dumps({"format": "tidal-runtime-v1",
        "files": {"member": release.digest(tmp_path / "member")}}))
    return tmp_path


@pytest.mark.parametrize("change", ["corrupt", "missing", "symlink"])
def test_changed_artifact_is_rejected_before_installer_runs(artifact, change, monkeypatch):
    monkeypatch.setattr(release, "run", lambda *_a, **_k: pytest.fail("Corrupt artifact cannot run"))
    member = artifact / "member"
    if change == "corrupt":
        member.write_bytes(b"truncated")
    else:
        member.unlink()
        if change == "symlink":
            member.symlink_to("electro-release.json")
    with pytest.raises(ValueError, match="member"):
        release.prepare(artifact)


def test_artifact_target_platform_is_required(artifact, monkeypatch):
    monkeypatch.setattr(release.platform, "machine", lambda: "arm64")
    with pytest.raises(ValueError, match="Linux x86_64"):
        release.verify(artifact)
