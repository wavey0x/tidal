"""Unit tests for AuctionKicker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from factory_dashboard.persistence import models
from factory_dashboard.persistence.repositories import KickTxRepository
import json

from factory_dashboard.pricing.token_price_agg import QuoteResult
from factory_dashboard.transaction_service.kicker import AuctionKicker, _DEFAULT_PRIORITY_FEE_GWEI
from factory_dashboard.transaction_service.types import KickCandidate, KickResult, PreparedKick


def _make_candidate(**overrides):
    defaults = {
        "source_type": "strategy",
        "source_address": "0x1111111111111111111111111111111111111111",
        "token_address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "auction_address": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "normalized_balance": "1000",
        "price_usd": "2.5",
        "want_address": "0xdddddddddddddddddddddddddddddddddddddddd",
        "usd_value": 2500.0,
        "decimals": 18,
    }
    defaults.update(overrides)
    return KickCandidate(**defaults)


def _make_prepared_kick(**overrides):
    candidate = _make_candidate(**(overrides.pop("candidate_overrides", {})))
    defaults = {
        "candidate": candidate,
        "sell_amount": 10**21,
        "starting_price_raw": 2750,
        "minimum_price_raw": 2375,
        "sell_amount_str": str(10**21),
        "starting_price_str": "2750",
        "minimum_price_str": "2375",
        "usd_value_str": "2500.0",
        "live_balance_raw": 10**21,
        "normalized_balance": "1000",
        "quote_amount_str": "2500",
    }
    defaults.update(overrides)
    return PreparedKick(**defaults)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    models.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def kick_tx_repo(session):
    return KickTxRepository(session)


def _make_kicker(session, *, web3_client=None, signer=None, price_provider=None, **overrides):
    if web3_client is None:
        web3_client = MagicMock()
    if signer is None:
        signer = MagicMock()
        signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
        signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    if price_provider is None:
        price_provider = AsyncMock()
        # Default: 2500 USDC (6 decimals) → with 10% buffer → startingPrice = 2750.
        price_provider.quote = AsyncMock(
            return_value=QuoteResult(amount_out_raw=2_500_000_000, token_out_decimals=6, provider_statuses={"curve": "ok"}, provider_amounts={"curve": 2_500_000_000})
        )

    kick_tx_repo = KickTxRepository(session)

    defaults = {
        "web3_client": web3_client,
        "signer": signer,
        "kick_tx_repository": kick_tx_repo,
        "price_provider": price_provider,
        "usd_threshold": 100.0,
        "max_base_fee_gwei": 0.5,
        "max_priority_fee_gwei": 2,
        "max_gas_limit": 500000,
        "start_price_buffer_bps": 1000,
        "min_price_buffer_bps": 500,
        "auction_kicker_address": "0x2a76c6aD151AF2EDbe16755Fc3BFf67176f01071",
        "chain_id": 1,
        "require_curve_quote": True,
    }
    defaults.update(overrides)
    return AuctionKicker(**defaults)


@pytest.mark.asyncio
async def test_kick_below_threshold_on_live_balance(session):
    """When live balance is below threshold, should return SKIP without persisting."""
    web3_client = MagicMock()

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        # Return a tiny balance → USD value below threshold.
        mock_erc20.read_balance = AsyncMock(return_value=1000)  # 1000 wei ≈ 0
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "SKIP"
    # No kick_txs row should be written for SKIP.
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_kick_balance_read_error(session):
    """When balance read fails, should persist ERROR row."""
    web3_client = MagicMock()

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(side_effect=RuntimeError("rpc down"))
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "ERROR"
    assert "rpc down" in result.error_message
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["status"] == "ERROR"
    assert "balance read failed" in rows[0]["error_message"]


@pytest.mark.asyncio
async def test_kick_base_fee_too_high(session):
    """When base fee exceeds limit, should persist ERROR."""
    web3_client = MagicMock()
    web3_client.get_balance = AsyncMock(return_value=int(1 * 1e18))  # 1 ETH
    web3_client.get_base_fee = AsyncMock(return_value=int(1 * 1e9))  # 1 gwei > 0.5 limit

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, max_base_fee_gwei=0.5)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "ERROR"
    assert "base fee" in result.error_message
    assert "exceeds limit" in result.error_message


@pytest.mark.asyncio
async def test_kick_estimate_failed(session):
    """When estimateGas fails, should persist ESTIMATE_FAILED."""
    web3_client = MagicMock()
    web3_client.get_balance = AsyncMock(return_value=int(1 * 1e18))
    web3_client.get_base_fee = AsyncMock(return_value=int(0.1 * 1e9))

    mock_contract = MagicMock()
    mock_kick_fn = MagicMock()
    mock_kick_fn._encode_transaction_data = MagicMock(return_value="0xdeadbeef")
    mock_contract.functions.batchKick = MagicMock(return_value=mock_kick_fn)
    web3_client.contract = MagicMock(return_value=mock_contract)
    web3_client.estimate_gas = AsyncMock(side_effect=RuntimeError("execution reverted"))

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "ESTIMATE_FAILED"
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["status"] == "ESTIMATE_FAILED"


@pytest.mark.asyncio
async def test_kick_gas_estimate_over_cap(session):
    """When gas estimate exceeds max_gas_limit, should persist ERROR."""
    web3_client = MagicMock()
    web3_client.get_balance = AsyncMock(return_value=int(1 * 1e18))
    web3_client.get_base_fee = AsyncMock(return_value=int(0.1 * 1e9))

    mock_contract = MagicMock()
    mock_kick_fn = MagicMock()
    mock_kick_fn._encode_transaction_data = MagicMock(return_value="0xdeadbeef")
    mock_contract.functions.batchKick = MagicMock(return_value=mock_kick_fn)
    web3_client.contract = MagicMock(return_value=mock_contract)
    web3_client.estimate_gas = AsyncMock(return_value=600000)  # exceeds 500000 cap

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "ERROR"
    assert "gas estimate" in result.error_message


@pytest.mark.asyncio
async def test_kick_confirmed(session):
    """Full happy path: estimate → sign → send → receipt confirmed."""
    web3_client = MagicMock()
    web3_client.get_balance = AsyncMock(return_value=int(1 * 1e18))
    web3_client.get_base_fee = AsyncMock(return_value=int(0.1 * 1e9))
    web3_client.get_max_priority_fee = AsyncMock(return_value=int(0.05 * 1e9))
    web3_client.estimate_gas = AsyncMock(return_value=200000)
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash123")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 12345,
    })

    mock_contract = MagicMock()
    mock_kick_fn = MagicMock()
    mock_kick_fn._encode_transaction_data = MagicMock(return_value="0xdeadbeef")
    mock_contract.functions.batchKick = MagicMock(return_value=mock_kick_fn)
    web3_client.contract = MagicMock(return_value=mock_contract)

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, signer=signer)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    assert result.tx_hash == "0xtxhash123"
    assert result.gas_used == 180000
    assert result.block_number == 12345

    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["status"] == "CONFIRMED"
    assert rows[0]["tx_hash"] == "0xtxhash123"


@pytest.mark.asyncio
async def test_kick_reverted(session):
    """Receipt shows reverted → status should be REVERTED."""
    web3_client = MagicMock()
    web3_client.get_balance = AsyncMock(return_value=int(1 * 1e18))
    web3_client.get_base_fee = AsyncMock(return_value=int(0.1 * 1e9))
    web3_client.get_max_priority_fee = AsyncMock(return_value=int(0.05 * 1e9))
    web3_client.estimate_gas = AsyncMock(return_value=200000)
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash456")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 0,
        "gasUsed": 150000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 12346,
    })

    mock_contract = MagicMock()
    mock_kick_fn = MagicMock()
    mock_kick_fn._encode_transaction_data = MagicMock(return_value="0xdeadbeef")
    mock_contract.functions.batchKick = MagicMock(return_value=mock_kick_fn)
    web3_client.contract = MagicMock(return_value=mock_contract)

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, signer=signer)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "REVERTED"
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert rows[0]["status"] == "REVERTED"


@pytest.mark.asyncio
async def test_kick_receipt_timeout_stays_submitted(session):
    """When receipt times out, row stays SUBMITTED."""
    web3_client = MagicMock()
    web3_client.get_balance = AsyncMock(return_value=int(1 * 1e18))
    web3_client.get_base_fee = AsyncMock(return_value=int(0.1 * 1e9))
    web3_client.get_max_priority_fee = AsyncMock(return_value=int(0.05 * 1e9))
    web3_client.estimate_gas = AsyncMock(return_value=200000)
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash789")
    web3_client.get_transaction_receipt = AsyncMock(side_effect=TimeoutError("receipt timeout"))

    mock_contract = MagicMock()
    mock_kick_fn = MagicMock()
    mock_kick_fn._encode_transaction_data = MagicMock(return_value="0xdeadbeef")
    mock_contract.functions.batchKick = MagicMock(return_value=mock_kick_fn)
    web3_client.contract = MagicMock(return_value=mock_contract)

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, signer=signer)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "SUBMITTED"
    assert result.tx_hash == "0xtxhash789"
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["status"] == "SUBMITTED"
    assert rows[0]["tx_hash"] == "0xtxhash789"


def _make_web3_client_through_gas_estimate(gas_estimate=200000):
    """Build a mock web3_client that passes all checks up through gas estimation."""
    web3_client = MagicMock()
    web3_client.get_balance = AsyncMock(return_value=int(1 * 1e18))
    web3_client.get_base_fee = AsyncMock(return_value=int(0.1 * 1e9))
    web3_client.get_max_priority_fee = AsyncMock(return_value=int(0.05 * 1e9))
    web3_client.estimate_gas = AsyncMock(return_value=gas_estimate)

    mock_contract = MagicMock()
    mock_kick_fn = MagicMock()
    mock_kick_fn._encode_transaction_data = MagicMock(return_value="0xdeadbeef")
    mock_contract.functions.batchKick = MagicMock(return_value=mock_kick_fn)
    web3_client.contract = MagicMock(return_value=mock_contract)
    return web3_client


@pytest.mark.asyncio
async def test_kick_confirm_fn_declined(session):
    """When confirm_fn returns False, should persist USER_SKIPPED and not send."""
    web3_client = _make_web3_client_through_gas_estimate()

    confirm_fn = MagicMock(return_value=False)

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, confirm_fn=confirm_fn)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "USER_SKIPPED"
    assert result.sell_amount is not None
    assert result.usd_value is not None

    # confirm_fn should have been called with a batch summary dict.
    confirm_fn.assert_called_once()
    summary = confirm_fn.call_args[0][0]
    assert "kicks" in summary
    assert "batch_size" in summary
    assert summary["batch_size"] == 1
    assert "gas_estimate" in summary
    assert "gas_limit" in summary

    kick = summary["kicks"][0]
    assert "strategy" in kick
    assert "strategy_name" in kick
    assert "token_symbol" in kick
    assert "want_symbol" in kick
    assert "starting_price_display" in kick
    assert "minimum_price" in kick
    assert "minimum_price_display" in kick
    assert "buffer_bps" in kick
    assert "min_buffer_bps" in kick
    assert isinstance(kick["buffer_bps"], int)
    assert isinstance(kick["min_buffer_bps"], int)
    # quote_amount is per-kick, gas fields are top-level.
    assert "quote_amount" in kick
    assert float(kick["quote_amount"]) > 0
    assert "base_fee_gwei" in summary
    assert "priority_fee_gwei" in summary
    assert "max_fee_per_gas_gwei" in summary
    assert "gas_cost_eth" in summary
    assert summary["base_fee_gwei"] == 0.1  # 0.1 gwei from mock
    assert summary["priority_fee_gwei"] == 0.05  # from mock
    assert summary["max_fee_per_gas_gwei"] == 2.5  # max(0.5, 0.1) + 2
    assert summary["gas_cost_eth"] == pytest.approx(200000 * 0.1 / 1e9)

    # Should NOT have tried to send.
    web3_client.get_transaction_count.assert_not_called()
    web3_client.send_raw_transaction.assert_not_called()

    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["status"] == "USER_SKIPPED"


@pytest.mark.asyncio
async def test_kick_confirm_fn_accepted(session):
    """When confirm_fn returns True, should proceed to sign and send."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_confirmed")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 99999,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    confirm_fn = MagicMock(return_value=True)

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, signer=signer, confirm_fn=confirm_fn)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    assert result.tx_hash == "0xtxhash_confirmed"
    confirm_fn.assert_called_once()


@pytest.mark.asyncio
async def test_kick_no_confirm_fn_sends_without_prompt(session):
    """When confirm_fn is None (default), should send without prompting."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_no_confirm")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 99999,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, signer=signer)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"


# ---------------------------------------------------------------------------
# Priority fee resolution tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_priority_fee_uses_network_suggestion_below_cap(session):
    """When the network-suggested priority fee is below the cap, use it."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_max_priority_fee = AsyncMock(return_value=int(0.03 * 1e9))  # 0.03 gwei
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_low_fee")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 55555,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, signer=signer, max_priority_fee_gwei=2)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    # The signed tx should use the network suggestion (0.03 gwei), not the cap (2 gwei).
    signed_tx_args = signer.sign_transaction.call_args[0][0]
    assert signed_tx_args["maxPriorityFeePerGas"] == int(0.03 * 1e9)


@pytest.mark.asyncio
async def test_priority_fee_capped_when_network_exceeds(session):
    """When the network-suggested priority fee exceeds the cap, use the cap."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_max_priority_fee = AsyncMock(return_value=int(5 * 1e9))  # 5 gwei > 2 gwei cap
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_capped")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 55556,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, signer=signer, max_priority_fee_gwei=2)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    signed_tx_args = signer.sign_transaction.call_args[0][0]
    assert signed_tx_args["maxPriorityFeePerGas"] == 2 * 10**9


@pytest.mark.asyncio
async def test_priority_fee_fallback_on_rpc_failure(session):
    """When the RPC call for max_priority_fee fails, fall back to default."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_max_priority_fee = AsyncMock(side_effect=RuntimeError("rpc error"))
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_fallback")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 55557,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, signer=signer, max_priority_fee_gwei=2)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    signed_tx_args = signer.sign_transaction.call_args[0][0]
    assert signed_tx_args["maxPriorityFeePerGas"] == int(_DEFAULT_PRIORITY_FEE_GWEI * 1e9)


# ---------------------------------------------------------------------------
# Starting price calculation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starting_price_usd_want_token(session):
    """With a $1 want token (USDC), startingPrice = lot value in want units, no WAD."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_sp1")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 70000,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    # Quote returns 2500 USDC (6 decimals) → with 10% buffer → 2750.
    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(amount_out_raw=2_500_000_000, token_out_decimals=6, provider_statuses={"curve": "ok"}, provider_amounts={"curve": 2_500_000_000})
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        # 1000 tokens with 18 decimals
        mock_erc20.read_balance = AsyncMock(return_value=1000 * 10**18)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(
            session, web3_client=web3_client, signer=signer,
            price_provider=price_provider,
            start_price_buffer_bps=1000,  # 10% buffer
        )
        # sell token $2.50, balance 1000 → quote returns 2500 USDC
        # startingPrice = ceil(2500 * 1.1) = 2750
        candidate = _make_candidate(
            price_usd="2.5", normalized_balance="1000",
        )
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    assert result.starting_price == "2750"


@pytest.mark.asyncio
async def test_starting_price_non_usd_want_token(session):
    """With WETH want token at $3000, startingPrice denominated in WETH units."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_sp2")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 70001,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    # Quote returns ~0.0797 WETH (18 decimals) → with 10% buffer → ceil = 1.
    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(amount_out_raw=79_666_666_666_666_667, token_out_decimals=18, provider_statuses={"curve": "ok"}, provider_amounts={"curve": 79_666_666_666_666_667})
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=1000 * 10**18)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(
            session, web3_client=web3_client, signer=signer,
            price_provider=price_provider,
            start_price_buffer_bps=1000,
        )
        # Quote gives ~0.0797 WETH → ceil(0.0797 * 1.1) = ceil(0.0877) = 1
        candidate = _make_candidate(
            price_usd="0.239", normalized_balance="1000",
        )
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    assert result.starting_price == "1"


@pytest.mark.asyncio
async def test_starting_price_ceil_ensures_nonzero(session):
    """Very small lot value should ceil to at least 1."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_sp3")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 70002,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    # Quote returns 10 raw USDC (6 decimals) = 0.00001 USDC → with 10% buffer → ceil = 1.
    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(amount_out_raw=10, token_out_decimals=6, provider_statuses={"curve": "ok"}, provider_amounts={"curve": 10})
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        # Very small balance: 0.001 tokens (1e15 raw with 18 decimals)
        mock_erc20.read_balance = AsyncMock(return_value=10**15)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(
            session, web3_client=web3_client, signer=signer,
            price_provider=price_provider,
            start_price_buffer_bps=1000,
            usd_threshold=0.0,  # disable threshold so tiny balance isn't skipped
        )
        # Quote gives 0.00001 USDC → ceil(0.00001 * 1.1) = ceil(0.000011) = 1
        candidate = _make_candidate(
            price_usd="0.01", normalized_balance="0.001",
        )
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    assert result.starting_price == "1"


@pytest.mark.asyncio
async def test_starting_price_ceiling_logs_precision_loss(session):
    """When ceiling inflates startingPrice >2x, a warning should be logged."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_prec")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 70010,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    # Quote returns ~0.065 want tokens (18 decimals) → ceil(0.065 * 1.1) = ceil(0.0715) = 1
    # 1 is >2x of 0.0715, so the warning should fire.
    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(amount_out_raw=65_000_000_000_000_000, token_out_decimals=18, provider_statuses={"curve": "ok"}, provider_amounts={"curve": 65_000_000_000_000_000})
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**20)  # 0.1 tokens
        MockERC20.return_value = mock_erc20

        with patch("factory_dashboard.transaction_service.kicker.logger") as mock_logger:
            kicker = _make_kicker(
                session, web3_client=web3_client, signer=signer,
                price_provider=price_provider,
                start_price_buffer_bps=1000,
                usd_threshold=0.0,
            )
            candidate = _make_candidate(
                price_usd="150.0", normalized_balance="0.1",
            )
            result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    assert result.starting_price == "1"

    # Verify the precision loss warning was logged.
    mock_logger.warning.assert_any_call(
        "txn_starting_price_precision_loss",
        source=candidate.source_address,
        token=candidate.token_address,
        exact_want_value=mock_logger.warning.call_args_list[0][1]["exact_want_value"],
        ceiled_value=1,
    )


@pytest.mark.asyncio
async def test_starting_price_no_precision_loss_warning_when_normal(session):
    """When ceiling doesn't inflate >2x, no precision loss warning is logged."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_noprec")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 70011,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    # Quote returns 2500 USDC → ceil(2500 * 1.1) = 2750. Not inflated.
    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(amount_out_raw=2_500_000_000, token_out_decimals=6, provider_statuses={"curve": "ok"}, provider_amounts={"curve": 2_500_000_000})
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=1000 * 10**18)
        MockERC20.return_value = mock_erc20

        with patch("factory_dashboard.transaction_service.kicker.logger") as mock_logger:
            kicker = _make_kicker(
                session, web3_client=web3_client, signer=signer,
                price_provider=price_provider,
                start_price_buffer_bps=1000,
            )
            candidate = _make_candidate(price_usd="2.5", normalized_balance="1000")
            result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    assert result.starting_price == "2750"

    # No precision loss warning should have been emitted.
    for call in mock_logger.warning.call_args_list:
        assert call[0][0] != "txn_starting_price_precision_loss"


@pytest.mark.asyncio
async def test_starting_price_real_tx_scenario(session):
    """Reproduce the real broken tx: sell ~$0.239 token to USDC, 1000 tokens.

    Old code: startingPrice = 262763600000000000 (WAD-scaled, astronomically wrong)
    New code: startingPrice = 263 (correct lot value in USDC units)
    """
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_sp4")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 70003,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    # Quote returns 239 USDC (6 decimals) → with 10% buffer → ceil(262.9) = 263.
    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(amount_out_raw=239_000_000, token_out_decimals=6, provider_statuses={"curve": "ok"}, provider_amounts={"curve": 239_000_000})
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=1000 * 10**18)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(
            session, web3_client=web3_client, signer=signer,
            price_provider=price_provider,
            start_price_buffer_bps=1000,  # 10%
        )
        # Quote gives 239 USDC → ceil(239 * 1.1) = ceil(262.9) = 263
        candidate = _make_candidate(
            price_usd="0.239", normalized_balance="1000",
        )
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    assert result.starting_price == "263"
    # Verify it's NOT the old broken WAD-scaled value
    assert int(result.starting_price) < 10**6  # sanity: reasonable integer, not 1e17


# ---------------------------------------------------------------------------
# Quote API error tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kick_quote_api_failure(session):
    """When quote API raises an exception, should persist ERROR."""
    web3_client = MagicMock()

    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(side_effect=RuntimeError("quote service down"))

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, price_provider=price_provider)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "ERROR"
    assert "quote service down" in result.error_message
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    assert "quote API failed" in rows[0]["error_message"]


@pytest.mark.asyncio
async def test_kick_quote_no_amount_out(session):
    """When quote returns None amount_out_raw, should persist ERROR."""
    web3_client = MagicMock()

    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(amount_out_raw=None, token_out_decimals=6, provider_statuses={"curve": "ok"}, provider_amounts={"curve": 0})
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, price_provider=price_provider)
        candidate = _make_candidate()
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "ERROR"
    assert "no quote available" in result.error_message
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    assert "no quote available" in rows[0]["error_message"]


# ---------------------------------------------------------------------------
# Minimum price derivation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minimum_price_derived_from_quote(session):
    """minimumPrice = floor(quote * 0.95) with default 500 bps (5%) buffer."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_mp1")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 80000,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    # Quote returns 2500 USDC (6 decimals).
    # startingPrice = ceil(2500 * 1.10) = 2750
    # minimumPrice  = floor(2500 * 0.95) = 2375
    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(amount_out_raw=2_500_000_000, token_out_decimals=6, provider_statuses={"curve": "ok"}, provider_amounts={"curve": 2_500_000_000})
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=1000 * 10**18)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(
            session, web3_client=web3_client, signer=signer,
            price_provider=price_provider,
            start_price_buffer_bps=1000,
            min_price_buffer_bps=500,
        )
        candidate = _make_candidate(price_usd="2.5", normalized_balance="1000")
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    assert result.starting_price == "2750"
    assert result.minimum_price == "2375"

    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["minimum_price"] == "2375"


@pytest.mark.asyncio
async def test_minimum_price_clamps_to_zero(session):
    """When quote is tiny, minimumPrice should clamp to 0 rather than go negative."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_mp2")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 80001,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    # Quote returns 10 raw USDC (6 decimals) = 0.00001 USDC.
    # floor(0.00001 * 0.95) = floor(0.0000095) = 0
    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(amount_out_raw=10, token_out_decimals=6, provider_statuses={"curve": "ok"}, provider_amounts={"curve": 10})
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**15)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(
            session, web3_client=web3_client, signer=signer,
            price_provider=price_provider,
            start_price_buffer_bps=1000,
            min_price_buffer_bps=500,
            usd_threshold=0.0,
        )
        candidate = _make_candidate(price_usd="0.01", normalized_balance="0.001")
        result = await kicker.kick(candidate, "run-1")

    assert result.status == "CONFIRMED"
    assert result.minimum_price == "0"


# ---------------------------------------------------------------------------
# prepare_kick tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_kick_success(session):
    """prepare_kick returns PreparedKick with correct computed prices."""
    web3_client = MagicMock()

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client)
        candidate = _make_candidate()
        result = await kicker.prepare_kick(candidate, "run-1")

    assert isinstance(result, PreparedKick)
    assert result.sell_amount == 10**21
    assert result.starting_price_str == "2750"
    assert result.minimum_price_str == "2375"
    assert result.candidate is candidate


@pytest.mark.asyncio
async def test_prepare_kick_below_threshold(session):
    """prepare_kick returns KickResult(SKIP) when below threshold."""
    web3_client = MagicMock()

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=1000)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client)
        candidate = _make_candidate()
        result = await kicker.prepare_kick(candidate, "run-1")

    assert isinstance(result, KickResult)
    assert result.status == "SKIP"


@pytest.mark.asyncio
async def test_prepare_kick_balance_error(session):
    """prepare_kick returns KickResult(ERROR) when balance read fails."""
    web3_client = MagicMock()

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(side_effect=RuntimeError("rpc down"))
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client)
        candidate = _make_candidate()
        result = await kicker.prepare_kick(candidate, "run-1")

    assert isinstance(result, KickResult)
    assert result.status == "ERROR"
    assert "rpc down" in result.error_message


@pytest.mark.asyncio
async def test_prepare_kick_quote_error(session):
    """prepare_kick returns KickResult(ERROR) when quote fails."""
    web3_client = MagicMock()

    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(side_effect=RuntimeError("quote down"))

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client, price_provider=price_provider)
        candidate = _make_candidate()
        result = await kicker.prepare_kick(candidate, "run-1")

    assert isinstance(result, KickResult)
    assert result.status == "ERROR"
    assert "quote" in result.error_message


# ---------------------------------------------------------------------------
# execute_batch tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_batch_confirmed(session):
    """Multiple prepared kicks all get CONFIRMED with same tx_hash."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_batch")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 500000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 90000,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    kicker = _make_kicker(session, web3_client=web3_client, signer=signer)

    pks = [
        _make_prepared_kick(candidate_overrides={"source_address": f"0x{'1' * 39}{i}"})
        for i in range(3)
    ]
    results = await kicker.execute_batch(pks, "run-batch")

    assert len(results) == 3
    for r in results:
        assert r.status == "CONFIRMED"
        assert r.tx_hash == "0xtxhash_batch"

    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 3
    assert all(row["tx_hash"] == "0xtxhash_batch" for row in rows)
    assert all(row["status"] == "CONFIRMED" for row in rows)


@pytest.mark.asyncio
async def test_execute_batch_reverted(session):
    """All kicks in a reverted batch get REVERTED."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_rev")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 0,
        "gasUsed": 300000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 90001,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    kicker = _make_kicker(session, web3_client=web3_client, signer=signer)

    pks = [_make_prepared_kick(), _make_prepared_kick()]
    results = await kicker.execute_batch(pks, "run-rev")

    assert len(results) == 2
    assert all(r.status == "REVERTED" for r in results)
    assert all(r.tx_hash == "0xtxhash_rev" for r in results)


@pytest.mark.asyncio
async def test_execute_batch_single_item(session):
    """Single-item batch works like the old single kick."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_single")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 200000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 90002,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    kicker = _make_kicker(session, web3_client=web3_client, signer=signer)

    results = await kicker.execute_batch([_make_prepared_kick()], "run-single")

    assert len(results) == 1
    assert results[0].status == "CONFIRMED"


@pytest.mark.asyncio
async def test_execute_batch_gas_over_cap(session):
    """Gas estimate exceeding scaled cap returns ERROR for all kicks."""
    web3_client = _make_web3_client_through_gas_estimate(gas_estimate=600000)

    kicker = _make_kicker(session, web3_client=web3_client)

    # Single kick → cap is 500000. Estimate 600000 > cap.
    results = await kicker.execute_batch([_make_prepared_kick()], "run-gas")

    assert len(results) == 1
    assert results[0].status == "ERROR"
    assert "gas estimate" in results[0].error_message


@pytest.mark.asyncio
async def test_execute_batch_confirm_declined(session):
    """Declining confirmation returns USER_SKIPPED for all kicks."""
    web3_client = _make_web3_client_through_gas_estimate()

    confirm_fn = MagicMock(return_value=False)

    kicker = _make_kicker(session, web3_client=web3_client, confirm_fn=confirm_fn)

    pks = [_make_prepared_kick(), _make_prepared_kick()]
    results = await kicker.execute_batch(pks, "run-decline")

    assert len(results) == 2
    assert all(r.status == "USER_SKIPPED" for r in results)
    confirm_fn.assert_called_once()


@pytest.mark.asyncio
async def test_execute_batch_receipt_timeout(session):
    """Receipt timeout leaves all kicks as SUBMITTED."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_timeout")
    web3_client.get_transaction_receipt = AsyncMock(side_effect=TimeoutError("timeout"))

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    kicker = _make_kicker(session, web3_client=web3_client, signer=signer)

    pks = [_make_prepared_kick(), _make_prepared_kick()]
    results = await kicker.execute_batch(pks, "run-timeout")

    assert len(results) == 2
    assert all(r.status == "SUBMITTED" for r in results)
    assert all(r.tx_hash == "0xtxhash_timeout" for r in results)


@pytest.mark.asyncio
async def test_execute_batch_confirm_summary_schema(session):
    """Verify the batch summary dict passed to confirm_fn has the expected shape."""
    web3_client = _make_web3_client_through_gas_estimate()
    confirm_fn = MagicMock(return_value=False)

    kicker = _make_kicker(session, web3_client=web3_client, confirm_fn=confirm_fn)

    pks = [_make_prepared_kick(), _make_prepared_kick()]
    await kicker.execute_batch(pks, "run-schema")

    confirm_fn.assert_called_once()
    summary = confirm_fn.call_args[0][0]

    # Batch-level keys.
    assert summary["batch_size"] == 2
    assert "total_usd" in summary
    assert "gas_estimate" in summary
    assert "gas_limit" in summary
    assert "base_fee_gwei" in summary
    assert "priority_fee_gwei" in summary
    assert "max_fee_per_gas_gwei" in summary
    assert "gas_cost_eth" in summary

    # Per-kick list.
    assert len(summary["kicks"]) == 2
    kick = summary["kicks"][0]
    assert "strategy" in kick
    assert "strategy_name" in kick
    assert "token_symbol" in kick
    assert "starting_price_display" in kick
    assert "minimum_price_display" in kick
    assert "quote_amount" in kick
    assert "buffer_bps" in kick
    assert "min_buffer_bps" in kick


# ---------------------------------------------------------------------------
# Curve quote requirement tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_kick_curve_unavailable_skips(session):
    """When require_curve_quote=True and Curve is down, prepare_kick returns ERROR."""
    web3_client = MagicMock()

    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(
            amount_out_raw=2_500_000_000,
            token_out_decimals=6,
            provider_statuses={"curve": "timeout", "defillama": "ok"},
            provider_amounts={"defillama": 2_500_000_000},
        )
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(
            session, web3_client=web3_client, price_provider=price_provider,
            require_curve_quote=True,
        )
        candidate = _make_candidate()
        result = await kicker.prepare_kick(candidate, "run-1")

    assert isinstance(result, KickResult)
    assert result.status == "ERROR"
    assert "curve quote unavailable" in result.error_message


@pytest.mark.asyncio
async def test_prepare_kick_curve_available_proceeds(session):
    """When require_curve_quote=True and Curve succeeded, prepare_kick returns PreparedKick."""
    web3_client = MagicMock()

    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(
            amount_out_raw=2_500_000_000,
            token_out_decimals=6,
            provider_statuses={"curve": "ok", "defillama": "ok"},
            provider_amounts={"curve": 2_500_000_000, "defillama": 2_400_000_000},
        )
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(
            session, web3_client=web3_client, price_provider=price_provider,
            require_curve_quote=True,
        )
        candidate = _make_candidate()
        result = await kicker.prepare_kick(candidate, "run-1")

    assert isinstance(result, PreparedKick)


@pytest.mark.asyncio
async def test_prepare_kick_curve_not_required_proceeds(session):
    """When require_curve_quote=False, Curve failure does not block the kick."""
    web3_client = MagicMock()

    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(
            amount_out_raw=2_500_000_000,
            token_out_decimals=6,
            provider_statuses={"curve": "timeout"},
            provider_amounts={},
        )
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(
            session, web3_client=web3_client, price_provider=price_provider,
            require_curve_quote=False,
        )
        candidate = _make_candidate()
        result = await kicker.prepare_kick(candidate, "run-1")

    assert isinstance(result, PreparedKick)


# ---------------------------------------------------------------------------
# Kick price audit logging tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_kick_persists_audit_columns(session):
    """A CONFIRMED kick should persist all price audit columns."""
    web3_client = _make_web3_client_through_gas_estimate()
    web3_client.get_transaction_count = AsyncMock(return_value=5)
    web3_client.send_raw_transaction = AsyncMock(return_value="0xtxhash_audit")
    web3_client.get_transaction_receipt = AsyncMock(return_value={
        "status": 1,
        "gasUsed": 180000,
        "effectiveGasPrice": int(12 * 1e9),
        "blockNumber": 12345,
    })

    signer = MagicMock()
    signer.address = "0xcccccccccccccccccccccccccccccccccccccccc"
    signer.checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    signer.sign_transaction = MagicMock(return_value=b"\x00" * 32)

    fake_response = {
        "summary": {"high_amount_out": "2500000000"},
        "providers": {
            "curve": {"status": "ok", "amount_out": 2_500_000_000},
            "defillama": {"status": "ok", "amount_out": 2_400_000_000},
        },
        "token_out": {"decimals": 6},
    }
    price_provider = AsyncMock()
    price_provider.quote = AsyncMock(
        return_value=QuoteResult(
            amount_out_raw=2_500_000_000,
            token_out_decimals=6,
            provider_statuses={"curve": "ok", "defillama": "ok"},
            raw_response=fake_response,
            provider_amounts={"curve": 2_500_000_000, "defillama": 2_400_000_000},
            request_url="https://prices.example.com/v1/quote?token_in=0xaaa&token_out=0xbbb&amount_in=1000&chain_id=1&use_underlying=true",
        )
    )

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(return_value=10**21)
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(
            session, web3_client=web3_client, signer=signer,
            price_provider=price_provider,
            start_price_buffer_bps=1000,
            min_price_buffer_bps=500,
        )
        candidate = _make_candidate(
            token_symbol="CRV",
            want_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            want_symbol="USDC",
        )
        result = await kicker.kick(candidate, "run-audit")

    assert result.status == "CONFIRMED"

    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    row = rows[0]

    assert row["quote_amount"] == "2500"
    assert row["quote_response_json"] is not None
    parsed = json.loads(row["quote_response_json"])
    assert parsed["summary"]["high_amount_out"] == "2500000000"
    assert "curve" in parsed["providers"]
    assert parsed["tokenOutDecimals"] == 6
    assert parsed["requestUrl"] == "https://prices.example.com/v1/quote?token_in=0xaaa&token_out=0xbbb&amount_in=1000&chain_id=1&use_underlying=true"
    assert "token_out" not in parsed
    assert "request_id" not in parsed

    assert row["start_price_buffer_bps"] == 1000
    assert row["min_price_buffer_bps"] == 500
    assert row["token_symbol"] == "CRV"
    assert row["want_address"] == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    assert row["want_symbol"] == "USDC"
    assert row["normalized_balance"] is not None


@pytest.mark.asyncio
async def test_pre_quote_error_has_null_pricing_columns(session):
    """An ERROR before the quote phase should leave pricing audit columns NULL."""
    web3_client = MagicMock()

    with patch(
        "factory_dashboard.transaction_service.kicker.ERC20Reader"
    ) as MockERC20:
        mock_erc20 = AsyncMock()
        mock_erc20.read_balance = AsyncMock(side_effect=RuntimeError("rpc down"))
        MockERC20.return_value = mock_erc20

        kicker = _make_kicker(session, web3_client=web3_client)
        candidate = _make_candidate(token_symbol="CRV", want_symbol="USDC")
        result = await kicker.kick(candidate, "run-prequote")

    assert result.status == "ERROR"

    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    row = rows[0]

    # Pricing columns should be NULL (no quote was fetched).
    assert row["quote_amount"] is None
    assert row["quote_response_json"] is None
    assert row["start_price_buffer_bps"] is None
    assert row["min_price_buffer_bps"] is None
    assert row["normalized_balance"] is None

    # Token identity columns should still be populated from the candidate.
    assert row["token_symbol"] == "CRV"
    assert row["want_symbol"] == "USDC"
