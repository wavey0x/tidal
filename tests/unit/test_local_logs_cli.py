"""Log commands read local retained history with no API dependency."""
import json

import pytest
import yaml
from sqlalchemy import text
from typer.testing import CliRunner

from tidal.cli import app
from tidal.cli_context import CLIContext
from tidal.config import load_settings
from tidal.migrations import run_migrations
from tidal.persistence import models
from tidal.persistence.db import Database
from tidal.resources import read_template_text


@pytest.fixture
def local_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("TIDAL_HOME", str(tmp_path))
    values = yaml.safe_load(read_template_text("server.yaml"))
    values["db_path"] = str(tmp_path / "tidal.db")
    values["rpc_url"] = None
    config = tmp_path / "server.yaml"
    config.write_text(yaml.safe_dump(values))
    settings = load_settings(config)
    run_migrations(settings.database_url)
    database = Database(settings.database_url)
    with database.session() as session:
        session.execute(models.scan_runs.insert().values(
            run_id="retained-scan", started_at="2026-09-13", finished_at="2026-09-13", status="SUCCESS",
            vaults_seen=1, strategies_seen=1, pairs_seen=1, pairs_succeeded=1, pairs_failed=0))
        session.execute(models.txn_runs.insert().values(
            run_id="retained-kick", started_at="2026-09-13", finished_at="2026-09-13", status="WAITING",
            candidates_found=1, kicks_attempted=1, kicks_succeeded=0, kicks_failed=0, live=1))
        session.execute(models.kick_txs.insert().values(
            run_id="retained-kick", operation_type="kick", auction_address="0x" + "1" * 40,
            token_address="0x" + "2" * 40, token_symbol="FIXTURE", created_at="2026-09-13",
            tx_hash="0x" + "ab" * 32, status="SUBMITTED"))
        session.commit()
    database.engine.dispose()
    return config


@pytest.mark.parametrize("args,key", [(("scans",), "items"), (("kicks",), "kicks")])
def test_local_logs_return_retained_data_without_rpc_or_http(local_logs, args, key):
    response = CliRunner().invoke(app, ["logs", *args, "--config", str(local_logs), "--json"])
    assert response.exit_code == 0, response.output
    assert len(json.loads(response.stdout)["data"][key]) == 1
    with CLIContext(local_logs).session(read_only=True) as session:
        assert session.execute(text("PRAGMA query_only")).scalar_one() == 1


@pytest.mark.parametrize("json_output", [False, True])
def test_local_run_detail_preserves_pending_transaction_identity(local_logs, json_output):
    response = CliRunner().invoke(app, ["logs", "show", "retained-kick", "--config", str(local_logs),
                                       *(["--json"] if json_output else [])])
    assert response.exit_code == 0, response.output
    assert "SUBMITTED" in response.stdout and "0x" + "ab" * 32 in response.stdout
