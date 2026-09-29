"""Native CLI contracts; sending and policy behavior are tested at their owners."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone
import fcntl

import json
import pytest
import yaml
from sqlalchemy import insert, select, update
from typer.testing import CliRunner

from tidal.cli import app
from tidal.config import load_server_settings
from tidal.migrations import run_migrations
from tidal.persistence import models
from tidal.persistence.db import Database
from tidal.resources import read_template_text
from tidal.transaction_service.types import TxnRunResult
import tidal.kick_cli as kick_cli


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setenv("TIDAL_HOME", str(tmp_path))
    values = yaml.safe_load(read_template_text("server.yaml"))
    values.update(db_path=str(tmp_path / "tidal.db"), rpc_url="https://example.invalid")
    config = tmp_path / "server.yaml"
    config.write_text(yaml.safe_dump(values))
    settings = load_server_settings(config)
    run_migrations(settings.database_url)
    captured = []
    unlocks = []

    def unlock(*args, **kwargs):
        unlocks.append(kwargs)
        return SimpleNamespace(signer=object(), sender="0x" + "1" * 40)

    def build(effective, session, **kwargs):
        async def run_once(**options):
            captured.append((effective, kwargs, options))
            return TxnRunResult(
                run_id="fixture-" + options["source_type"], status="DRY_RUN" if not options["live"] else "SUCCESS",
                candidates_found=0, kicks_attempted=0, kicks_succeeded=0, kicks_failed=0,
            )
        return SimpleNamespace(run_once=run_once)

    monkeypatch.setattr(kick_cli.CLIContext, "resolve_execution", unlock)
    assert not hasattr(kick_cli.CLIContext, "control_plane_client")
    monkeypatch.setattr(kick_cli, "build_txn_service", build)
    return SimpleNamespace(config=config, captured=captured, unlocks=unlocks, settings=settings)


def invoke(native, *args):
    return CliRunner().invoke(app, ["kick", "run", "--config", str(native.config), *args])


@pytest.fixture
def cooldown_rows(native):
    auction = "0x" + "2" * 40
    tokens = ["0x" + value * 40 for value in "34567"]
    now = datetime.now(timezone.utc)
    database = Database(native.settings.database_url)
    with database.session() as session:
        for token, target, when in [
            (tokens[0], auction, now - timedelta(hours=1)),
            (tokens[0], auction, now),
            (tokens[1], auction, now),
            (tokens[2], auction, now),
            (tokens[0], "0x" + "8" * 40, now),
            (tokens[3], auction, now - timedelta(days=2)),
        ]:
            session.execute(insert(models.kick_txs).values(
                run_id="cooldown", token_address=token, auction_address=target,
                status="CONFIRMED", created_at=when.isoformat(),
            ))
        session.commit()
    database.engine.dispose()
    return auction, tokens


def read_cooldowns(native):
    database = Database(native.settings.database_url)
    try:
        with database.session() as session:
            return [dict(row) for row in session.execute(select(models.kick_txs).order_by(models.kick_txs.c.id)).mappings()]
    finally:
        database.engine.dispose()


def clear_cooldowns(native, auction, tokens, *args):
    selectors = [item for token in tokens for item in ("--token", token)]
    return CliRunner().invoke(app, ["kick", "clear-cooldown", "--config", str(native.config),
                                  "--auction", auction, *selectors, "--json", *args])


def test_cooldown_clear_preview_batch_isolation_persistence_and_idempotence(native, cooldown_rows):
    auction, tokens = cooldown_rows
    selected = [tokens[0], tokens[1], tokens[0], tokens[3], tokens[4]]
    before = read_cooldowns(native)
    preview = clear_cooldowns(native, auction, selected, "--dry-run")
    assert preview.exit_code == 0, preview.output
    assert [row["status"] for row in json.loads(preview.stdout)["data"]["tokens"]] == [
        "would_clear", "would_clear", "no_active_cooldown", "no_active_cooldown",
    ]
    assert read_cooldowns(native) == before
    response = clear_cooldowns(native, auction, selected)
    assert response.exit_code == 0, response.output
    assert [row["status"] for row in json.loads(response.stdout)["data"]["tokens"]] == [
        "cleared", "cleared", "no_active_cooldown", "no_active_cooldown",
    ]
    after = read_cooldowns(native)
    for index, (old, new) in enumerate(zip(before, after, strict=True)):
        if index in (1, 2):
            assert new["cooldown_cleared_at"] is not None
            assert {**new, "cooldown_cleared_at": None} == old
        else:
            assert new == old
    repeated = clear_cooldowns(native, auction, selected)
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(repeated.stdout)["data"]["tokens"][0]["status"] == "already_cleared"
    assert read_cooldowns(native) == after
    assert native.unlocks == native.captured == []


def test_cooldown_clear_validates_whole_batch_before_writing(native, cooldown_rows):
    auction, tokens = cooldown_rows
    before = read_cooldowns(native)
    response = clear_cooldowns(native, auction, [tokens[0], "invalid"])
    assert response.exit_code == 2
    assert read_cooldowns(native) == before


def test_cooldown_clear_rolls_back_whole_batch_on_failure(native, cooldown_rows, monkeypatch):
    auction, tokens = cooldown_rows
    before = read_cooldowns(native)
    original = kick_cli.KickTxRepository.update_fields
    calls = []
    def fail_second(self, kick_id, **values):
        calls.append(kick_id)
        if len(calls) == 2:
            raise RuntimeError("write failure")
        original(self, kick_id, **values)
    monkeypatch.setattr(kick_cli.KickTxRepository, "update_fields", fail_second)
    response = clear_cooldowns(native, auction, tokens[:2])
    assert response.exit_code == 1
    assert len(calls) == 2
    assert read_cooldowns(native) == before


def test_cooldown_clear_respects_runner_execution_lock(native, cooldown_rows):
    auction, tokens = cooldown_rows
    before = read_cooldowns(native)
    with (native.settings.resolved_home_path / "execution.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        response = clear_cooldowns(native, auction, tokens[:2])
    assert response.exit_code == 75, response.output
    assert json.loads(response.stdout)["code"] == "BUSY"
    assert read_cooldowns(native) == before


@pytest.mark.parametrize("selectors", [[], ["--auction", "0x" + "2" * 40], ["--token", "0x" + "3" * 40]])
def test_cooldown_clear_requires_exact_auction_and_tokens(selectors):
    response = CliRunner().invoke(app, ["kick", "clear-cooldown", *selectors])
    assert response.exit_code == 2


def test_native_kick_preserves_distinct_scheduled_profiles_and_needs_no_api(native):
    response = invoke(native, "--headless", "--json")
    assert response.exit_code == 0, response.output
    payload = json.loads(response.stdout)
    assert payload["interface_version"] == 1
    assert len(payload["data"]["runs"]) == 2
    assert [(settings.txn_usd_threshold, settings.txn_base_fee_cap_gwei, settings.txn_require_curve_quote)
            for settings, _, _ in native.captured] == [(250, 1, True), (50, 1, False)]
    assert [settings.txn_max_gas_limit for settings, _, _ in native.captured] == [2500000, 2500000]
    assert all(settings.txn_data_freshness_limit_seconds == 1200 for settings, _, _ in native.captured)
    assert [options["source_type"] for _, _, options in native.captured] == ["strategy", "fee_burner"]
    assert all(options["batch"] is False for _, _, options in native.captured)
    assert len(native.unlocks) == 1


@pytest.mark.parametrize("source,threshold,curve", [("strategy", 250, True), ("fee-burner", 50, False)])
def test_selected_profile_preserves_policy(native, source, threshold, curve):
    response = invoke(native, "--headless", "--json", "--source-type", source)
    assert response.exit_code == 0, response.output
    assert len(native.captured) == 1
    settings, _, options = native.captured[0]
    assert settings.txn_usd_threshold == threshold
    assert settings.txn_require_curve_quote is curve
    assert options["source_type"] == source.replace("-", "_")


def test_manual_fee_quote_and_value_overrides_reach_native_service(native):
    response = invoke(native, "--headless", "--json", "--source-type", "strategy",
                      "--min-usd-value", "175", "--max-base-fee-gwei", "0.5", "--no-require-curve")
    assert response.exit_code == 0, response.output
    settings, _, _ = native.captured[0]
    assert (settings.txn_usd_threshold, settings.txn_base_fee_cap_gwei, settings.txn_require_curve_quote) == (175, 0.5, False)
    # Applying an override must not mutate the shared base or other profile.
    assert native.settings.execution_profiles["strategy"].txn_usd_threshold == 250


def test_dry_run_never_unlocks_any_key(native):
    response = invoke(native, "--dry-run", "--json")
    assert response.exit_code == 0, response.output
    assert native.unlocks == []
    assert all(kwargs["signer"] is None and options["live"] is False for _, kwargs, options in native.captured)


def test_json_send_requires_deliberate_unattended_flag(native):
    response = invoke(native, "--json")
    assert response.exit_code == 2
    assert native.unlocks == []


@pytest.mark.parametrize("option", ["--min-usd-value", "--max-base-fee-gwei"])
def test_negative_policy_override_is_rejected_before_unlock(native, option):
    response = invoke(native, "--headless", option, "-1")
    assert response.exit_code == 2
    assert native.unlocks == []


def test_no_fill_override_requires_exact_pair_and_remains_disallowed_in_headless(native):
    for args in [("--allow-no-fill-retry",),
                 ("--allow-no-fill-retry", "--headless", "--auction", "0x" + "2" * 40, "--token", "0x" + "3" * 40)]:
        response = invoke(native, *args)
        assert response.exit_code == 2, response.output
    assert native.unlocks == []


def test_manual_guard_overrides_reach_native_planner(native):
    response = invoke(native, "--no-confirmation", "--json", "--source-type", "strategy",
                      "--allow-killed-gauge", "--allow-no-fill-retry",
                      "--auction", "0x" + "2" * 40, "--token", "0x" + "3" * 40)
    assert response.exit_code == 0, response.output
    _, _, options = native.captured[0]
    assert options["allow_killed_gauge"] and options["allow_no_fill_retry"]


def test_help_describes_local_execution_and_no_remote_control_plane_options():
    response = CliRunner().invoke(app, ["kick", "run", "--help"])
    assert response.exit_code == 0
    assert "--api-key" not in response.output
    assert "--api-base-url" not in response.output
    assert "--dry-run" in response.output


def test_missing_profile_is_explicit_instead_of_silently_inheriting_other_policy(native):
    native.settings.execution_profiles.pop("fee_burner")
    with pytest.raises(Exception, match="Missing explicit execution profile"):
        kick_cli._profile_settings(native.settings, "fee_burner")


@pytest.mark.parametrize("failed,dependencies,expected_exit,code", [
    (1, 1, 75, "WAITING_FOR_DEPENDENCY"),
    (2, 1, 1, "EXECUTION_ERROR"),
    (1, 0, 1, "EXECUTION_ERROR"),
])
def test_dependency_wait_preserves_errors_and_does_not_mask_execution_failure(native, monkeypatch, failed, dependencies, expected_exit, code):
    def build(*args, **kwargs):
        async def run_once(**options):
            return TxnRunResult(run_id="quote-fixture", status="FAILED", candidates_found=failed,
                kicks_attempted=0, kicks_succeeded=0, kicks_failed=failed,
                dependency_failures=dependencies, failure_summary={"quote unavailable": dependencies})
        return SimpleNamespace(run_once=run_once)
    monkeypatch.setattr(kick_cli, "build_txn_service", build)
    response = invoke(native, "--headless", "--json", "--source-type", "strategy")
    assert response.exit_code == expected_exit, response.output
    payload = json.loads(response.stdout)
    assert payload["code"] == code
    assert payload["data"]["runs"][0]["kicks_failed"] == failed
    assert payload["blockers"]


def retained(session, source, *, status="CONFIRMED", signer="0x" + "1" * 40, profile="kick", chain_id=1):
    transaction_id = session.execute(insert(models.transactions).values(
        operation="kick", status=status, signer=signer, profile=profile, chain_id=chain_id,
        created_at="2026-09-28T00:00:00+00:00", updated_at="2026-09-28T00:00:00+00:00",
    )).lastrowid
    session.execute(insert(models.kick_txs).values(
        run_id="retained", source_type=source, token_address="0xtoken", auction_address="0xauction",
        status="SUBMITTED", transaction_id=transaction_id, created_at="2026-09-28T00:00:00+00:00",
    ))
    session.commit()
    return transaction_id


def outcome(status="SUCCESS", attempted=0):
    return TxnRunResult(
        run_id="fixture", status=status, candidates_found=attempted,
        kicks_attempted=attempted, kicks_succeeded=0, kicks_failed=0,
    )


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("status", ["BUSY", "WAITING"])
def test_deferred_pass_returns_without_retrying_either_source(native, monkeypatch, json_output, status):
    calls = []
    def build(*args, **kwargs):
        async def run_once(**options):
            calls.append(options["source_type"])
            return outcome(status)
        return SimpleNamespace(run_once=run_once)
    monkeypatch.setattr(kick_cli, "build_txn_service", build)
    response = invoke(native, "--headless", *(["--json"] if json_output else []))
    assert response.exit_code == 75, response.output
    assert calls == ["strategy", "fee_burner"]
    if json_output:
        payload = json.loads(response.stdout)
        assert payload["code"] == "WAITING"
        assert len(payload["data"]["runs"]) == 2
    else:
        assert response.stdout.count("Execution deferred") == 2
        assert "No eligible candidates" not in response.stdout


@pytest.mark.parametrize("json_output", [False, True])
def test_next_timer_pass_gives_other_source_a_turn_after_confirmation(native, monkeypatch, json_output):
    calls = []
    def build(effective, session, **kwargs):
        async def run_once(**options):
            source = options["source_type"]
            calls.append(source)
            if session.execute(select(models.transactions).where(
                models.transactions.c.status == "PENDING",
            )).first():
                return outcome("WAITING")
            retained(session, source, status="PENDING")
            return outcome("WAITING", attempted=1)
        return SimpleNamespace(run_once=run_once)
    monkeypatch.setattr(kick_cli, "build_txn_service", build)
    args = ["--headless", *(["--json"] if json_output else [])]
    first = invoke(native, *args)
    assert first.exit_code == 75, first.output
    assert calls == ["strategy", "fee_burner"]
    calls.clear()
    # An intervening timer tick must not duplicate the retained submission.
    pending = invoke(native, *args)
    assert pending.exit_code == 75, pending.output
    assert calls == ["fee_burner", "strategy"]
    with Database(native.settings.database_url).session() as session:
        assert len(session.execute(select(models.transactions)).all()) == 1
        session.execute(update(models.transactions).values(status="CONFIRMED"))
        session.commit()
    calls.clear()
    confirmed = invoke(native, *args)
    assert confirmed.exit_code == 75, confirmed.output
    assert calls == ["fee_burner", "strategy"]
    with Database(native.settings.database_url).session() as session:
        assert session.execute(select(models.kick_txs.c.source_type).order_by(
            models.kick_txs.c.id,
        )).scalars().all() == ["strategy", "fee_burner"]


def test_next_pass_uses_retained_submission_order_even_after_restart(native):
    with Database(native.settings.database_url).session() as session:
        retained(session, "strategy")
        # Scanner operations and other signers/chains cannot consume a turn.
        retained(session, "fee_burner", profile="scan")
        retained(session, "fee_burner", signer="0x" + "2" * 40)
        retained(session, "fee_burner", chain_id=2)
    response = invoke(native, "--headless", "--json")
    assert response.exit_code == 0, response.output
    assert [options["source_type"] for _, _, options in native.captured] == ["fee_burner", "strategy"]
    with Database(native.settings.database_url).session() as session:
        retained(session, "fee_burner")
    native.captured.clear()
    response = invoke(native, "--headless", "--json")
    assert response.exit_code == 0, response.output
    assert [options["source_type"] for _, _, options in native.captured] == ["strategy", "fee_burner"]
