"""Unit tests for scanner-side auction resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import tidal.scanner.auction_settler as auction_settler_module
from tidal.auction_settlement import AuctionLotPreview, AuctionSettlementInspection
from tidal.persistence import models
from tidal.persistence.repositories import KickTxRepository
from tidal.scanner.auction_settler import AuctionSettlementService, AuctionSource
from tidal.types import TokenMetadata


class _FakeResolveFunction:
    def __init__(self, auction, token, force):  # noqa: ANN001
        self.auction = auction
        self.token = token
        self.force = force

    def _encode_transaction_data(self) -> bytes:
        return f"resolve:{self.auction}:{self.token}:{self.force}".encode()


class _FakeKickerFunctions:
    def resolveAuction(self, auction, token, force):  # noqa: ANN001
        return _FakeResolveFunction(auction, token, force)


class _FakeKickerContract:
    def __init__(self) -> None:
        self.functions = _FakeKickerFunctions()


class _FakeWeb3Client:
    def __init__(self, *, estimate_error: Exception | None = None) -> None:
        self.estimate_error = estimate_error
        self.sent = 0

    def contract(self, address, abi):  # noqa: ARG002
        return _FakeKickerContract()

    async def get_base_fee(self) -> int:
        return int(0.1 * 1e9)

    async def get_max_priority_fee(self) -> int:
        return int(1 * 1e9)

    async def get_transaction_count(self, address):  # noqa: ARG002
        return 7

    async def estimate_gas(self, tx):  # noqa: ARG002
        if self.estimate_error is not None:
            raise self.estimate_error
        return 100_000

    async def send_raw_transaction(self, signed_tx):  # noqa: ARG002
        self.sent += 1
        return "0xresolvehash"

    async def get_transaction_receipt(self, tx_hash, *, timeout_seconds=120):  # noqa: ARG002
        return {
            "status": 1,
            "gasUsed": 123456,
            "effectiveGasPrice": 300000000,
            "blockNumber": 999,
        }


class _FakeTokenMetadataService:
    async def get_or_fetch(self, token_address, is_core_reward=False):  # noqa: ARG002
        token = token_address.lower()
        symbol = {
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": "OPASF",
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": "crvUSD",
        }.get(token, "UNK")
        now = datetime.now(timezone.utc).isoformat()
        return TokenMetadata(
            address=token,
            chain_id=1,
            name=symbol,
            symbol=symbol,
            decimals=18,
            is_core_reward=False,
            first_seen_at=now,
            last_seen_at=now,
        )


class _FakeSigner:
    address = "0xcccccccccccccccccccccccccccccccccccccccc"
    checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"

    def sign_transaction(self, tx):  # noqa: ARG002
        return b"signed"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    models.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _make_settler(session, *, web3_client):
    return AuctionSettlementService(
        web3_client=web3_client,
        signer=_FakeSigner(),
        kick_tx_repository=KickTxRepository(session),
        token_metadata_service=_FakeTokenMetadataService(),
        base_fee_cap_gwei=0.5,
        max_priority_fee_gwei=2,
        max_gas_limit=500000,
        chain_id=1,
        settings=SimpleNamespace(
            auction_kicker_address="0x9999999999999999999999999999999999999999",
            multicall_address="0x8888888888888888888888888888888888888888",
            multicall_enabled=True,
            multicall_auction_batch_calls=100,
        ),
    )


def _inspection(*previews: AuctionLotPreview) -> AuctionSettlementInspection:
    return AuctionSettlementInspection(
        auction_address="0x1111111111111111111111111111111111111111",
        is_active_auction=any(preview.active for preview in previews),
        enabled_tokens=tuple(preview.token_address for preview in previews),
        requested_token=None,
        lot_previews=previews,
    )


def _preview(
    *,
    token: str,
    path: int,
    active: bool,
    balance_raw: int,
    requires_force: bool = False,
    read_ok: bool = True,
    error_message: str | None = None,
) -> AuctionLotPreview:
    return AuctionLotPreview(
        token_address=token,
        path=path if read_ok else None,
        active=active if read_ok else None,
        kicked_at=123 if read_ok else None,
        balance_raw=balance_raw if read_ok else None,
        requires_force=requires_force if read_ok else None,
        receiver="0x7777777777777777777777777777777777777777" if read_ok else None,
        read_ok=read_ok,
        error_message=error_message,
    )


@pytest.mark.asyncio
async def test_settler_ignores_inactive_kicked_empty_lots(session, monkeypatch) -> None:
    auction = "0x1111111111111111111111111111111111111111"
    token = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    want = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    web3_client = _FakeWeb3Client()
    settler = _make_settler(session, web3_client=web3_client)

    monkeypatch.setattr(
        auction_settler_module,
        "inspect_auction_settlements",
        AsyncMock(
            return_value={
                auction: _inspection(_preview(token=token, path=4, active=False, balance_raw=0)),
            }
        ),
    )

    result = await settler.settle_stale_auctions(
        run_id="run-1",
        sources=[AuctionSource("fee_burner", "0xburner", auction, want)],
    )

    assert result.stats.eligible_tokens == 0
    assert result.stats.settlements_attempted == 0
    assert result.stats.settlements_confirmed == 0
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert rows == []


@pytest.mark.asyncio
async def test_settler_skips_live_funded_blocker(session, monkeypatch) -> None:
    auction = "0x1111111111111111111111111111111111111111"
    token = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    web3_client = _FakeWeb3Client()
    settler = _make_settler(session, web3_client=web3_client)

    monkeypatch.setattr(
        auction_settler_module,
        "inspect_auction_settlements",
        AsyncMock(
            return_value={
                auction: _inspection(
                    _preview(token=token, path=3, active=True, balance_raw=123, requires_force=True)
                ),
            }
        ),
    )

    result = await settler.settle_stale_auctions(
        run_id="run-1",
        sources=[AuctionSource("fee_burner", "0xburner", auction, "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")],
    )

    assert result.stats.blocking_tokens == 1
    assert result.stats.settlements_attempted == 0
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert rows == []


@pytest.mark.asyncio
async def test_settler_skips_auction_with_preview_failure(session, monkeypatch) -> None:
    auction = "0x1111111111111111111111111111111111111111"
    token = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    web3_client = _FakeWeb3Client()
    settler = _make_settler(session, web3_client=web3_client)

    monkeypatch.setattr(
        auction_settler_module,
        "inspect_auction_settlements",
        AsyncMock(
            return_value={
                auction: _inspection(
                    _preview(
                        token=token,
                        path=0,
                        active=False,
                        balance_raw=0,
                        read_ok=False,
                        error_message="multicall preview failed",
                    )
                ),
            }
        ),
    )

    result = await settler.settle_stale_auctions(
        run_id="run-1",
        sources=[AuctionSource("fee_burner", "0xburner", auction, "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")],
    )

    assert len(result.errors) == 1
    assert result.errors[0].error_code == "resolve_preview_failed"
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert rows == []
