"""Native CLI contracts; sending and policy behavior are tested at their owners."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import json
import fcntl
import pytest
import yaml
from sqlalchemy import insert, select, update
from typer.testing import CliRunner

from tidal.cli import app
from tidal.config import load_server_settings
from tidal.migrations import run_migrations
from tidal.lifecycle import execution_lock
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


def outcome(code=None, attempted=0):
    return TxnRunResult(
        run_id="fixture", status="BUSY" if code == "BUSY" else "WAITING" if code else "SUCCESS",
        candidates_found=attempted, kicks_attempted=attempted, kicks_succeeded=0, kicks_failed=0,
        blocked_code=code,
    )


def test_hourly_cycle_retries_contention_then_waits_for_finality_between_sources(native, monkeypatch):
    calls, sleeps, sessions = [], [], []
    now = [0]
    monkeypatch.setattr(kick_cli, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def build(effective, session, **kwargs):
        sessions.append(session)
        async def run_once(**options):
            source = options["source_type"]
            calls.append(source)
            with execution_lock(effective.resolved_home_path / "execution.lock"):
                if len(calls) == 1:
                    return outcome("BUSY")
                if source == "strategy":
                    retained(session, source, status="PENDING")
                    # A partial pass has already sent; it must not be repeated.
                    return outcome("UNRESOLVED_ATTEMPTS", attempted=1)
                if len(calls) == 3:
                    return outcome("UNRESOLVED_ATTEMPTS")
                assert session.execute(select(models.transactions.c.status)).scalar_one() == "CONFIRMED"
                return outcome()
        return SimpleNamespace(run_once=run_once)

    async def sleep(seconds):
        assert all(not session.in_transaction() for session in sessions)
        with (native.settings.resolved_home_path / "execution.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Another worker can both acquire execution and write while we wait.
            with Database(native.settings.database_url).session() as session:
                session.execute(update(models.transactions).values(status="CONFIRMED"))
                session.commit()
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(kick_cli, "build_txn_service", build)
    monkeypatch.setattr(kick_cli.asyncio, "sleep", sleep)
    response = invoke(native, "--headless", "--wait-seconds", "90", "--json")
    assert response.exit_code == 0, response.output
    assert calls == ["strategy", "strategy", "fee_burner", "fee_burner"]
    assert sleeps == [15, 15]
    payload = json.loads(response.stdout)
    assert len(payload["data"]["runs"]) == 2
    assert payload["data"]["pending_transactions"] == 0


@pytest.mark.parametrize("code,status", [("BUSY", None), ("UNRESOLVED_ATTEMPTS", "INCLUDED")])
def test_hourly_cycle_has_one_shared_deadline(native, monkeypatch, code, status):
    now, calls = [0], []
    monkeypatch.setattr(kick_cli, "time", SimpleNamespace(monotonic=lambda: now[0]))
    def build(effective, session, **kwargs):
        if status:
            retained(session, "strategy", status=status)
        async def run_once(**options):
            calls.append(options["source_type"])
            return outcome(code)
        return SimpleNamespace(run_once=run_once)
    async def sleep(seconds):
        now[0] += seconds
    monkeypatch.setattr(kick_cli, "build_txn_service", build)
    monkeypatch.setattr(kick_cli.asyncio, "sleep", sleep)
    response = invoke(native, "--headless", "--wait-seconds", "20", "--json")
    assert response.exit_code == 75, response.output
    payload = json.loads(response.stdout)
    assert now[0] == 20 and calls == ["strategy", "strategy"]
    assert payload["data"]["remaining_profiles"] == ["strategy", "fee_burner"]
    assert any(row["code"] == "CYCLE_TIME_LIMIT" for row in payload["blockers"])


def test_review_required_is_never_automatically_retried(native, monkeypatch):
    with Database(native.settings.database_url).session() as session:
        retained(session, "strategy", status="REVIEW_REQUIRED")
    calls = []
    def build(*args, **kwargs):
        async def run_once(**options):
            calls.append(options["source_type"])
            return outcome("UNRESOLVED_ATTEMPTS")
        return SimpleNamespace(run_once=run_once)
    async def sleep(seconds):
        pytest.fail("review must not be retried")
    monkeypatch.setattr(kick_cli, "build_txn_service", build)
    monkeypatch.setattr(kick_cli.asyncio, "sleep", sleep)
    response = invoke(native, "--headless", "--wait-seconds", "90", "--json")
    assert response.exit_code == 75, response.output
    assert calls == ["fee_burner", "strategy"]


def test_next_hour_uses_retained_submission_order_even_after_restart(native):
    with Database(native.settings.database_url).session() as session:
        retained(session, "strategy")
        # Scanner operations and other signers/chains cannot consume a turn.
        retained(session, "fee_burner", profile="scan")
        retained(session, "fee_burner", signer="0x" + "2" * 40)
        retained(session, "fee_burner", chain_id=2)
    response = invoke(native, "--headless", "--wait-seconds", "90", "--json")
    assert response.exit_code == 0, response.output
    assert [options["source_type"] for _, _, options in native.captured] == ["fee_burner", "strategy"]
    with Database(native.settings.database_url).session() as session:
        retained(session, "fee_burner")
    native.captured.clear()
    response = invoke(native, "--headless", "--wait-seconds", "90", "--json")
    assert response.exit_code == 0, response.output
    assert [options["source_type"] for _, _, options in native.captured] == ["strategy", "fee_burner"]


@pytest.mark.parametrize("args", [[], ["--dry-run"], ["--headless", "--dry-run"]])
def test_continuation_requires_live_headless_before_unlock(native, args):
    response = invoke(native, "--wait-seconds", "30", *args)
    assert response.exit_code == 2
    assert native.unlocks == []
