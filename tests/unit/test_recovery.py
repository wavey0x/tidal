"""Recovery boundaries: real SQLite, fixture identity, and no external effects."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import tidal.recovery as recovery
import tidal.runtime as wiring
from tidal.alerts.base import AlertMessage, NullAlertSink
from tidal.alerts.dispatcher import AlertDispatcher
from tidal.lifecycle import LifecycleError
from tidal.persistence import models
from tidal.scanner.service import ScannerService
from tidal.time import utcnow_iso
from tidal.types import ScanRunResult
from tests.unit.test_managed_execution import runtime, submit, TOKEN, AUCTION


@pytest.fixture
def recovery_runtime(runtime, monkeypatch, tmp_path):
    key = tmp_path / "fixture-keystore.json"
    key.write_text(json.dumps({"address": runtime.signer.address}))
    runtime.settings.txn_keystore_path = str(key)
    runtime.rpc.close = AsyncMock()
    monkeypatch.setattr(recovery, "build_web3_client", lambda _: runtime.rpc)
    return runtime


@pytest.mark.asyncio
async def test_resume_binds_current_nonce_without_unlock_send_or_price(recovery_runtime, monkeypatch):
    state = recovery_runtime
    monkeypatch.setattr(wiring, "TransactionSigner", lambda *a, **k: pytest.fail("resume cannot unlock"))
    monkeypatch.setattr(wiring, "TokenPriceAggProvider", lambda *a, **k: pytest.fail("resume cannot price"))
    result = await recovery.resume(state.settings, state.session)
    activation = json.loads((state.settings.resolved_home_path / "activation.json").read_text())
    assert result["code"] == "RUNNING"
    assert activation["nonce_baseline"] == {state.signer.address: 7}
    assert activation["signers"] == state.settings.managed_signers
    assert state.signer.calls == state.rpc.sends == 0
    state.rpc.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_unrecorded_mempool_activity_refuses_resume_and_clears_activation(recovery_runtime):
    state = recovery_runtime
    state.rpc.pending_nonce = 8
    with pytest.raises(LifecycleError, match="Unrecorded pending"):
        await recovery.resume(state.settings, state.session)
    assert not (state.settings.resolved_home_path / "activation.json").exists()
    assert state.signer.calls == state.rpc.sends == 0


@pytest.mark.asyncio
async def test_known_pending_attempt_allows_observation_but_blocks_sender(recovery_runtime):
    state = recovery_runtime
    await submit(state)
    result = await recovery.resume(state.settings, state.session)
    assert result["code"] == "RUNNING" and result["warnings"][0]["code"] == "UNRESOLVED_ATTEMPTS"
    report = await recovery.status(state.settings, state.session, web3=state.rpc)
    assert report["data"]["pending_count"] == 1
    assert all(not item["may_send"] for item in report["data"]["managed_signers"].values())
    assert state.rpc.sends == 1


@pytest.mark.asyncio
async def test_rpc_outage_does_not_prevent_api_or_cached_status(recovery_runtime):
    state = recovery_runtime
    state.rpc.get_chain_id = AsyncMock(side_effect=TimeoutError)
    report = await recovery.status(state.settings, state.session, web3=state.rpc)
    assert report["data"]["api_can_serve"]
    assert not report["data"]["chain_reads_ready"]
    assert report["code"] == "WAITING_FOR_RPC"
    assert state.rpc.sends == state.signer.calls == 0


@pytest.mark.parametrize("kind", ["stale", "wrong_chain", "wrong_signer"])
@pytest.mark.asyncio
async def test_resume_refuses_unready_dependencies(recovery_runtime, kind):
    state = recovery_runtime
    if kind == "stale":
        state.rpc.timestamp = 1
    elif kind == "wrong_chain":
        state.rpc.get_chain_id = AsyncMock(return_value=2)
    else:
        state.settings.managed_signers["kick"] = "0x" + "9" * 40
    with pytest.raises(LifecycleError):
        await recovery.resume(state.settings, state.session)
    assert not (state.settings.resolved_home_path / "activation.json").exists()


def test_restore_preparation_rotates_access_stales_prices_and_preserves_history(recovery_runtime, tmp_path):
    state = recovery_runtime
    session = state.session
    session.execute(models.tokens.insert().values(address=TOKEN, chain_id=1, decimals=18, first_seen_at="old", last_seen_at="old",
        price_usd="123456789.123456789", price_status="SUCCESS", price_fetched_at="original-price-time"))
    session.execute(models.api_keys.insert().values(label="old", key_hash="old-hash", key_prefix="old", created_at="old"))
    session.execute(models.kick_txs.insert().values(run_id="old", operation_type="kick", token_address=TOKEN,
        auction_address=AUCTION, status="CONFIRMED", created_at="original-time", historical_baseline=1, historical_baseline_reason="preserved",
        requested_sell_amount="99999999999999999999999999999999999"))
    session.commit()
    before = dict(session.execute(select(models.kick_txs)).mappings().one())
    credential = tmp_path / "restored-access.json"
    result = recovery.prepare_restore(state.settings, session, credential_file=credential)
    key = json.loads(credential.read_text())["api_key"]
    assert key not in json.dumps(result)
    assert credential.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "activation.json").exists()
    assert dict(session.execute(select(models.kick_txs)).mappings().one()) == before
    token = session.execute(select(models.tokens)).mappings().one()
    assert token["price_fetched_at"] == "original-price-time" and token["price_status"] == "STALE"
    assert token["price_usd"] == "123456789.123456789"
    keys = session.execute(select(models.api_keys)).mappings().all()
    assert keys[0]["revoked_at"] is not None
    assert keys[1]["key_hash"] == hashlib.sha256(key.encode()).hexdigest()
    assert session.execute(select(models.app_metadata.c.notification_baseline_pending)).scalar_one() == 1


def test_credential_output_cannot_replace_database_or_configuration(recovery_runtime, tmp_path):
    state = recovery_runtime
    for path in [state.path, tmp_path / "important.yaml"]:
        if path != state.path:
            path.write_text("rpc_url: important")
        original = path.read_bytes()
        with pytest.raises(LifecycleError, match="credential|Credential"):
            recovery.prepare_restore(state.settings, state.session, credential_file=path)
        assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_recovery_composition_creates_no_effect_capable_clients(recovery_runtime, monkeypatch):
    state = recovery_runtime
    state.settings.txn_keystore_passphrase = "fixture-only"
    monkeypatch.setattr(wiring, "Web3Client", lambda *a, **k: state.rpc)
    for name in ["TransactionSigner", "TokenPriceAggProvider", "ManagedExecutor", "AuctionSettlementService",
                 "AuctionTokenEnablementService", "build_alert_sink", "AlertDispatcher", "AuctionScanService"]:
        monkeypatch.setattr(wiring, name, lambda *a, **k: pytest.fail("Recovery instantiated an effect-capable service"))
    scanner = wiring.build_scanner_service(state.settings, state.session, auto_settle=True, auto_enable_tokens=True, recovery=True)
    assert scanner.auction_settler is scanner.auction_token_enabler is None
    assert scanner.alert_dispatcher is scanner.alert_service is scanner.operation_reconciler is None
    assert scanner.token_price_refresh_service.price_provider is None
    assert not scanner.token_price_refresh_service.enabled
    await scanner.close()


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.asyncio
async def test_refresh_requires_complete_reads_and_does_not_clear_notification_baseline(recovery_runtime, monkeypatch, failed):
    state = recovery_runtime
    state.session.execute(models.app_metadata.update().values(notification_baseline_pending=1))
    state.session.commit()
    scan = ScanRunResult(run_id="recovery-fixture", status="SUCCESS", vaults_seen=0, strategies_seen=0,
                         pairs_seen=0, pairs_succeeded=0, pairs_failed=0)
    async def scan_once():
        if failed:
            state.session.execute(models.scan_item_errors.insert().values(run_id=scan.run_id,
                stage="BALANCE_READ", error_code="RPC_ERROR", error_message="fixture failure", created_at=utcnow_iso()))
            state.session.commit()
        return scan
    scanner = SimpleNamespace(web3_client=state.rpc, scan_once=scan_once, close=AsyncMock())
    monkeypatch.setattr(recovery, "build_scanner_service", lambda *a, **k: scanner)
    outcome = await recovery.refresh_recovery(state.settings, state.session)
    metadata = state.session.execute(select(models.app_metadata)).mappings().one()
    assert outcome["code"] == ("WAITING_FOR_DEPENDENCY" if failed else "REFRESHED")
    assert bool(metadata["recovery_refreshed_at"]) == (not failed)
    assert metadata["notification_baseline_pending"] == 1
    scanner.close.assert_awaited_once()


def message(key="old-transition"):
    return AlertMessage(delivery_key=key, occurrence_id=key, severity="warning",
                        title="Fixture", summary="Fixture", retry_at=None, links=())


@pytest.mark.asyncio
async def test_suppression_is_not_success_and_old_notifications_stay_muted(recovery_runtime):
    session = recovery_runtime.session
    sink = SimpleNamespace(destination_codes=("admin_alerts", "operations_alerts"), send=AsyncMock())
    dispatcher = AlertDispatcher(session=session, sink=sink)
    await dispatcher.dispatch((message(),), suppress=True)
    await dispatcher.dispatch((message(),))
    sink.send.assert_not_awaited()
    rows = session.execute(select(models.alert_deliveries)).mappings().all()
    assert all(row["suppressed_at"] and row["sent_at"] is None and row["attempt_count"] == 0 for row in rows)
    await dispatcher.dispatch((message("new-transition"),))
    assert sink.send.await_count == 2


@pytest.mark.asyncio
async def test_baseline_waits_for_first_complete_normal_scan(recovery_runtime):
    session = recovery_runtime.session
    session.execute(models.app_metadata.update().values(notification_baseline_pending=1))
    session.commit()
    sink = SimpleNamespace(destination_codes=("admin_alerts",), send=AsyncMock())
    scanner = SimpleNamespace(session=session, alert_service=SimpleNamespace(
        evaluate=lambda: SimpleNamespace(transitions=(message(),))),
        alert_dispatcher=AlertDispatcher(session=session, sink=sink),
        token_price_refresh_service=SimpleNamespace(enabled=False))
    await ScannerService._post_commit_alerts(scanner)
    assert session.execute(select(models.app_metadata.c.notification_baseline_pending)).scalar_one() == 1
    scanner.token_price_refresh_service.enabled = True
    await ScannerService._post_commit_alerts(scanner, complete=False)
    assert session.execute(select(models.app_metadata.c.notification_baseline_pending)).scalar_one() == 1
    await ScannerService._post_commit_alerts(scanner)
    assert session.execute(select(models.app_metadata.c.notification_baseline_pending)).scalar_one() == 0
    sink.send.assert_not_awaited()
