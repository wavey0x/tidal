from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tidal.kick_prepare_status import record_kick_prepare_status
from tidal.persistence import models


KEY = {
    "sourceType": "strategy",
    "sourceAddress": "0x1111111111111111111111111111111111111111",
    "auctionAddress": "0x2222222222222222222222222222222222222222",
    "tokenAddress": "0x3333333333333333333333333333333333333333",
}


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)
    return Session(engine, future=True)


def test_precision_pause_is_replaced_and_cleared_by_successful_prepare() -> None:
    session = _session()
    record_kick_prepare_status(
        session,
        {
            "preparedOperations": [],
            "skippedDuringPrepare": [
                {
                    **KEY,
                    "reasonCode": "AUCTION_PRICE_GRANULARITY",
                    "reasonData": {
                        "sourceBalanceRaw": "1000",
                        "floorQuoteAmountRaw": "91",
                        "terminalAskRaw": "115",
                        "wantDecimals": 18,
                    },
                }
            ],
        },
    )

    row = session.execute(select(models.kick_prepare_status_latest)).mappings().one()
    assert row["status"] == "PAUSED"
    assert row["reason"] == "AUCTION_PRICE_GRANULARITY"
    assert row["source_balance_raw"] == "1000"
    assert '"terminalAskRaw": "115"' in row["detail_json"]

    record_kick_prepare_status(
        session,
        {
            "preparedOperations": [{**KEY, "operation": "kick"}],
            "skippedDuringPrepare": [],
        },
    )

    assert session.execute(select(models.kick_prepare_status_latest)).first() is None
    session.close()
