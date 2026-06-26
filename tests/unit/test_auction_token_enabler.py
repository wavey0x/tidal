from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tidal.constants import YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS
from tidal.persistence import models
from tidal.persistence.repositories import AuctionEnabledTokenRepository, KickTxRepository
from tidal.scanner.auction_token_enabler import (
    AuctionEnableCandidate,
    AuctionEnableSource,
    AuctionTokenEnablementService,
)


AUCTION = "0x1111111111111111111111111111111111111111"
SOURCE = "0x2222222222222222222222222222222222222222"
WANT = "0x3333333333333333333333333333333333333333"
KICKER = "0x4444444444444444444444444444444444444444"
TOKEN_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TOKEN_C = "0xcccccccccccccccccccccccccccccccccccccccc"


class _FakeAuctionEnableFunction:
    def __init__(self, token: str) -> None:
        self.token = token.lower()

    def _encode_transaction_data(self) -> bytes:
        return f"auction-enable:{self.token}".encode()


class _FakeMetadataFunction:
    def __init__(self, auction: str, field: str) -> None:
        self.auction = auction.lower()
        self.field = field


class _FakeKickerEnableFunction:
    def __init__(self, auction: str, tokens: list[str]) -> None:
        self.auction = auction.lower()
        self.tokens = [token.lower() for token in tokens]

    def _encode_transaction_data(self) -> bytes:
        return f"kicker-enable:{self.auction}:{','.join(self.tokens)}".encode()


class _FakeAuctionFunctions:
    def __init__(self, auction: str) -> None:
        self.auction = auction.lower()

    def governance(self) -> _FakeMetadataFunction:
        return _FakeMetadataFunction(self.auction, "governance")

    def want(self) -> _FakeMetadataFunction:
        return _FakeMetadataFunction(self.auction, "want")

    def receiver(self) -> _FakeMetadataFunction:
        return _FakeMetadataFunction(self.auction, "receiver")

    def enable(self, token: str) -> _FakeAuctionEnableFunction:
        return _FakeAuctionEnableFunction(token)


class _FakeKickerFunctions:
    def enableTokens(self, auction: str, tokens: list[str]) -> _FakeKickerEnableFunction:
        return _FakeKickerEnableFunction(auction, tokens)


class _FakeContract:
    def __init__(self, functions) -> None:  # noqa: ANN001
        self.functions = functions


class _FakeWeb3Client:
    def __init__(
        self,
        *,
        metadata: dict[str, dict[str, str]] | None = None,
        base_fee_wei: int = 100_000_000,
        estimate_error: Exception | None = None,
        send_error: Exception | None = None,
        receipt_status: int = 1,
    ) -> None:
        self.metadata = metadata or {
            AUCTION: {
                "governance": YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS.lower(),
                "want": WANT,
                "receiver": SOURCE,
            }
        }
        self.base_fee_wei = base_fee_wei
        self.estimate_error = estimate_error
        self.send_error = send_error
        self.receipt_status = receipt_status
        self.sent_data: list[bytes] = []

    def contract(self, address: str, abi):  # noqa: ANN001, ARG002
        if address.lower() == KICKER:
            return _FakeContract(_FakeKickerFunctions())
        return _FakeContract(_FakeAuctionFunctions(address))

    async def call(self, fn):  # noqa: ANN001
        return self.metadata[fn.auction][fn.field]

    async def eth_call_raw(
        self,
        target: str,
        call_data: bytes,
        *,
        from_address: str | None = None,
        block_identifier="latest",
    ):  # noqa: ANN001
        del target, call_data, from_address, block_identifier
        return b""

    async def get_base_fee(self) -> int:
        return self.base_fee_wei

    async def get_max_priority_fee(self) -> int:
        return 1_000_000_000

    async def get_transaction_count(self, address: str) -> int:
        del address
        return len(self.sent_data)

    async def estimate_gas(self, tx: dict[str, object]) -> int:
        if self.estimate_error is not None:
            raise self.estimate_error
        data = bytes(tx["data"])
        tokens = data.decode().split(":", 2)[2].split(",")
        return 300_000 * len(tokens)

    async def send_raw_transaction(self, signed_tx: bytes) -> str:
        if self.send_error is not None:
            raise self.send_error
        self.sent_data.append(signed_tx)
        return f"0x{len(self.sent_data):064x}"

    async def get_transaction_receipt(self, tx_hash: str, *, timeout_seconds: int = 120):  # noqa: ANN001
        del tx_hash, timeout_seconds
        return {
            "status": self.receipt_status,
            "gasUsed": 123456,
            "effectiveGasPrice": 300_000_000,
            "blockNumber": 999,
        }


class _FakeAuctionStateReader:
    def __init__(self, enabled: dict[tuple[str, str], bool | None] | None = None) -> None:
        self.enabled = enabled or {}

    async def read_auction_token_enabled_many(
        self,
        pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], bool | None]:
        return {
            (auction, token): self.enabled.get((auction, token), False)
            for auction, token in pairs
        }


class _FakeSigner:
    address = "0x5555555555555555555555555555555555555555"
    checksum_address = "0x5555555555555555555555555555555555555555"

    def sign_transaction(self, tx: dict[str, object]) -> bytes:
        return bytes(tx["data"])


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    models.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _source(*, factory_verified: bool = True) -> AuctionEnableSource:
    return AuctionEnableSource(
        source_type="strategy",
        source_address=SOURCE,
        auction_address=AUCTION,
        want_address=WANT,
        factory_verified=factory_verified,
    )


def _candidate(token: str, *, source: AuctionEnableSource | None = None) -> AuctionEnableCandidate:
    return AuctionEnableCandidate(
        source=source or _source(),
        token_address=token,
        decimals=18,
        balance_raw=10**18,
        normalized_balance="1",
        token_symbol="TOK",
    )


def _service(session, *, web3_client: _FakeWeb3Client | None = None, state_reader=None):  # noqa: ANN001
    return AuctionTokenEnablementService(
        web3_client=web3_client or _FakeWeb3Client(),
        auction_state_reader=state_reader or _FakeAuctionStateReader(),
        signer=_FakeSigner(),
        kick_tx_repository=KickTxRepository(session),
        auction_enabled_token_repository=AuctionEnabledTokenRepository(session),
        base_fee_cap_gwei=0.5,
        max_priority_fee_gwei=2,
        max_gas_limit=500_000,
        chain_id=1,
        settings=SimpleNamespace(auction_kicker_address=KICKER),
    )


@pytest.mark.asyncio
async def test_auto_enable_confirms_token_and_updates_cache(session) -> None:
    web3_client = _FakeWeb3Client()
    service = _service(session, web3_client=web3_client)

    result = await service.enable_missing_tokens(
        run_id="run-1",
        candidates=[_candidate(TOKEN_A)],
        enabled_tokens_by_auction={AUCTION: set()},
    )

    assert result.errors == []
    assert result.stats.eligible_tokens == 1
    assert result.stats.tokens_confirmed == 1
    assert len(web3_client.sent_data) == 1

    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["operation_type"] == "enable_tokens"
    assert rows[0]["status"] == "CONFIRMED"
    assert rows[0]["auction_address"] == AUCTION
    assert rows[0]["token_address"] == TOKEN_A

    enabled_rows = session.execute(select(models.auction_enabled_tokens_latest)).mappings().all()
    assert len(enabled_rows) == 1
    assert enabled_rows[0]["auction_address"] == AUCTION
    assert enabled_rows[0]["token_address"] == TOKEN_A
    assert enabled_rows[0]["active"] == 1


@pytest.mark.asyncio
async def test_auto_enable_skips_unverified_and_already_enabled_sources(session) -> None:
    web3_client = _FakeWeb3Client()
    service = _service(session, web3_client=web3_client)

    result = await service.enable_missing_tokens(
        run_id="run-1",
        candidates=[
            _candidate(TOKEN_A, source=_source(factory_verified=False)),
            _candidate(TOKEN_B),
        ],
        enabled_tokens_by_auction={AUCTION: {TOKEN_B}},
    )

    assert result.errors == []
    assert result.stats.skipped_unverified_sources == 1
    assert result.stats.already_enabled_tokens == 1
    assert result.stats.eligible_tokens == 0
    assert web3_client.sent_data == []
    assert session.execute(select(models.kick_txs)).mappings().all() == []


@pytest.mark.asyncio
async def test_auto_enable_splits_batches_over_gas_cap(session) -> None:
    web3_client = _FakeWeb3Client()
    service = _service(session, web3_client=web3_client)

    result = await service.enable_missing_tokens(
        run_id="run-1",
        candidates=[_candidate(TOKEN_A), _candidate(TOKEN_B), _candidate(TOKEN_C)],
        enabled_tokens_by_auction={AUCTION: set()},
    )

    assert result.errors == []
    assert result.stats.eligible_tokens == 3
    assert result.stats.enable_transactions_confirmed == 3
    assert result.stats.tokens_confirmed == 3
    assert len(web3_client.sent_data) == 3

    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 3
    assert {row["status"] for row in rows} == {"CONFIRMED"}


@pytest.mark.asyncio
async def test_auto_enable_skips_when_base_fee_is_high(session) -> None:
    web3_client = _FakeWeb3Client(base_fee_wei=2_000_000_000)
    service = _service(session, web3_client=web3_client)

    result = await service.enable_missing_tokens(
        run_id="run-1",
        candidates=[_candidate(TOKEN_A)],
        enabled_tokens_by_auction={AUCTION: set()},
    )

    assert result.stats.eligible_tokens == 1
    assert result.stats.skipped_high_base_fee is True
    assert web3_client.sent_data == []
    assert session.execute(select(models.kick_txs)).mappings().all() == []
