from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tidal.persistence import models
from tidal.persistence.repositories import KickTxRepository


def test_cooldown_uses_latest_unsent_quote_failure_only():
    engine = create_engine("sqlite:///:memory:")
    models.metadata.create_all(engine)
    with Session(engine) as session:
        repo = KickTxRepository(session)
        base = dict(run_id="test", operation_type="kick", token_address="token", auction_address="auction",
                    status="ERROR", created_at="2026-11-18T12:00:00+00:00")
        def add(**changes):
            session.execute(models.kick_txs.insert().values(**(base | changes)))
        assert repo.last_unsent_quote_failure("auction", "token") is None
        add(quote_response_json='{"providers":{"curve":{"status":"error"}}}')
        assert repo.last_unsent_quote_failure("auction", "token") is not None
        assert repo.last_unsent_quote_failure("another-auction", "token") is None
        add(status="CONFIRMED", tx_hash="0xabc")
        assert repo.last_unsent_quote_failure("auction", "token") is None
        add(error_message="quote API failed: timed out")
        assert repo.last_unsent_quote_failure("auction", "token") is not None
        add(error_message="RPC failed before pricing")
        assert repo.last_unsent_quote_failure("auction", "token") is None
        add(quote_response_json="{}", tx_hash="0xabc")
        assert repo.last_unsent_quote_failure("auction", "token") is None
