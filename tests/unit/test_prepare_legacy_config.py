"""The one-time policy conversion preserves actual server/CLI responsibilities."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from dotenv import dotenv_values

spec = importlib.util.spec_from_file_location("prepare_legacy_config", Path(__file__).parents[2] / "scripts/prepare_legacy_config.py")
prepare_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_module)


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    from tidal.config import Settings
    config = tmp_path / "old.yml"
    config.write_text("kick: {}\n")
    key = tmp_path / "original-key.json"
    key.write_text('{"crypto":"retained ciphertext"}')
    settings = Settings().model_dump(mode="json")
    settings.update(txn_usd_threshold=250, txn_max_gas_limit=500000,
        txn_data_freshness_limit_seconds=1200, txn_base_fee_cap_gwei=5,
        rpc_url="https://private.example/credential", txn_keystore_passphrase="private-pass")
    row = {"settings": settings, "signer": "0x" + "1" * 40,
        "keystore_path": str(key), "config_path": str(config), "secret_path": str(tmp_path / "old.env")}
    old = {"scan": row, "kick": {**row, "settings": {**settings,
        "txn_usd_threshold": 100, "txn_max_gas_limit": 2500000, "txn_data_freshness_limit_seconds": 600}}}
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "-c" in args:
            return SimpleNamespace(returncode=0, stdout=json.dumps(old))
        assert args[1:] == ["-I", "-m", "tidal.cli", "check-config", "--json"]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"interface_version": 1,
            "code": "CONFIGURATION_VALID", "data": {"signers": {name: row["signer"] for name in ("scan", "kick")}}}))
    monkeypatch.setattr(prepare_module.subprocess, "run", run)
    return old, calls, key


def test_effective_policy_and_secrets_are_preserved_without_public_disclosure(tmp_path, legacy):
    old, calls, key = legacy
    output, destination = tmp_path / "candidate", tmp_path / "installed"
    result = prepare_module.prepare("/old/python", tmp_path, tmp_path / ".tidal", output, destination)
    policy = yaml.safe_load((output / "server.yml").read_text())
    profiles = policy["execution_profiles"]
    assert profiles["strategy"]["txn_usd_threshold"] == 250
    assert profiles["fee_burner"]["txn_usd_threshold"] == 50
    assert [profiles[name]["txn_max_gas_limit"] for name in ("scan", "strategy", "fee_burner")] == [500000, 2500000, 2500000]
    assert policy["txn_data_freshness_limit_seconds"] == 1200
    assert (output / "server-keystore.json").read_bytes() == key.read_bytes()
    private = dotenv_values(output / "server.env")
    assert private["TXN_KEYSTORE_PASSPHRASE"] == "private-pass"
    assert private["RPC_URL"] == old["scan"]["settings"]["rpc_url"]
    assert "private-pass" not in json.dumps(result) + json.dumps(policy)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())
    assert calls[1][1]["env"]["TXN_KEYSTORE_PATH"] == str(output / "server-keystore.json")


@pytest.mark.parametrize("difference", ["identity", "policy"])
def test_unexpected_identity_or_policy_difference_refuses_before_creating_candidate(tmp_path, legacy, difference):
    old, calls, _ = legacy
    if difference == "identity":
        old["kick"]["signer"] = "0x" + "2" * 40
    else:
        old["kick"]["settings"]["txn_max_priority_fee_gwei"] = 999
    with pytest.raises(ValueError):
        prepare_module.prepare("/old/python", tmp_path, tmp_path / ".tidal", tmp_path / "candidate", tmp_path / "installed")
    assert not (tmp_path / "candidate").exists()
    assert len(calls) == 1
