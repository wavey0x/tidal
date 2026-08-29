from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tidal.persistence import models
from tidal.persistence.repositories import FeeBurnerRepository


def test_failed_refresh_preserves_last_good_fee_burner_mapping() -> None:
    engine = create_engine("sqlite:///:memory:")
    models.metadata.create_all(engine)
    burner = "0x1111111111111111111111111111111111111111"
    auction = "0x2222222222222222222222222222222222222222"
    with Session(engine) as session:
        session.execute(
            models.fee_burners.insert().values(
                address=burner,
                chain_id=1,
                active=1,
                auction_address=auction,
                auction_version="1.0.4",
                first_seen_at="2026-08-29T00:00:00Z",
                last_seen_at="2026-08-29T00:00:00Z",
            )
        )
        repository = FeeBurnerRepository(session)

        repository.mark_auction_refresh_failed(
            {burner: "factory read failed"},
            updated_at="2026-08-29T01:00:00Z",
        )

        row = session.execute(
            select(models.fee_burners).where(models.fee_burners.c.address == burner)
        ).mappings().one()
        assert row["auction_address"] == auction
        assert row["auction_version"] == "1.0.4"
        assert row["auction_error_message"] == "factory read failed"
