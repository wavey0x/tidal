import pytest
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tidal.api.app import create_app
from tidal.config import Settings
from tidal.persistence import models
from test_api_control_plane import _init_db


def test_api_cannot_write_database_and_never_loads_signer_or_starts_reconciler(tmp_path, monkeypatch):
    settings = Settings(DB_PATH=tmp_path / "tidal.db", RPC_URL="https://example.invalid",
                        TXN_KEYSTORE_PATH=str(tmp_path / "absent-key.json"), TXN_KEYSTORE_PASSPHRASE="fixture-only")
    _init_db(settings)
    # API fixtures normally create only model tables. Health also requires
    # the explicit lifecycle metadata supplied by migrations in a real DB.
    from tidal.lifecycle import SCHEMA_REVISION
    app_engine = create_engine(settings.database_url)
    with Session(app_engine) as session:
        session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        session.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": SCHEMA_REVISION})
        session.execute(models.app_metadata.insert().values(id=1, database_identity=str(uuid.uuid4())))
        session.commit()
    app_engine.dispose()
    monkeypatch.setattr("tidal.runtime.TransactionSigner", lambda *a, **k: pytest.fail("API must not unlock a signer"))
    monkeypatch.setattr("tidal.runtime.build_web3_client", lambda *a, **k: pytest.fail("API lifespan must not start receipt RPC"))
    app = create_app(settings)
    assert app.state.settings.txn_keystore_path is None
    assert app.state.settings.txn_keystore_passphrase is None
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        with app.state.database.session() as session:
            assert session.execute(text("PRAGMA query_only")).scalar_one() == 1
            with pytest.raises(OperationalError):
                session.execute(text("DELETE FROM api_keys"))
        # Old clients cannot register or alter transaction outcomes.
        for endpoint in ("broadcast", "receipt"):
            response = client.post(f"/api/v1/tidal/actions/old/{endpoint}",
                                   headers={"Authorization": "Bearer secret-token"}, json={"receiptStatus": "CONFIRMED"})
            assert response.status_code == 404


def test_api_transaction_view_reads_same_ledger_and_preserves_provisional_status(tmp_path):
    from sqlalchemy import create_engine
    settings = Settings(DB_PATH=tmp_path / "tidal.db")
    _init_db(settings)
    with Session(create_engine(settings.database_url)) as session:
        transaction_id = session.execute(models.transactions.insert().values(
            operation="kick", profile="kick", legacy=0, chain_id=1, signer="0x" + "1" * 40,
            nonce=7, tx_hash="0x" + "ab" * 32, to_address="0x" + "2" * 40,
            data="0x1234", value="0", status="INCLUDED", created_at="2026-09-13", updated_at="2026-09-13",
        )).lastrowid
        session.execute(models.kick_txs.insert().values(
            transaction_id=transaction_id, run_id="fixture", operation_type="kick",
            token_address="0x" + "3" * 40, auction_address="0x" + "4" * 40,
            status="SUBMITTED", created_at="2026-09-13",
        ))
        session.commit()
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer secret-token"}
        assert client.get("/api/v1/tidal/transactions").status_code == 401
        data = client.get("/api/v1/tidal/transactions?status=INCLUDED", headers=headers).json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == transaction_id
        detail = client.get(f"/api/v1/tidal/transactions/{transaction_id}", headers=headers).json()["data"]
        assert detail["status"] == "INCLUDED"
        assert detail["operations"][0]["status"] == "SUBMITTED"

@pytest.mark.parametrize("state", ["missing", "incompatible"])
def test_health_refuses_unusable_database_without_contacting_dependencies(tmp_path, state, monkeypatch):
    from tidal.migrations import run_migrations
    settings = Settings(DB_PATH=tmp_path / "health.db")
    if state == "incompatible":
        run_migrations(settings.database_url)
        engine = create_engine(settings.database_url)
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num='other-release'"))
        engine.dispose()
    monkeypatch.setattr("tidal.runtime.build_web3_client", lambda *a, **k: pytest.fail("Health cannot need RPC"))
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
        assert response.status_code == 503 and response.json()["data"]["ready"] is False
    if state == "missing":
        assert not settings.resolved_db_path.exists()


@pytest.mark.parametrize("field", ["TXN_KEYSTORE_PATH", "TXN_KEYSTORE_PASSPHRASE"])
def test_api_serve_refuses_inherited_signing_credentials(monkeypatch, field):
    from typer.testing import CliRunner
    from tidal.cli import app
    monkeypatch.setenv(field, "fixture-sensitive-value")
    monkeypatch.setattr("tidal.server_cli.uvicorn.run", lambda *a, **k: pytest.fail("API must not start with signing credentials"))
    response = CliRunner().invoke(app, ["api", "serve"])
    assert response.exit_code == 2
    assert "Remove signing credentials" in response.output
    assert "fixture-sensitive-value" not in response.output
