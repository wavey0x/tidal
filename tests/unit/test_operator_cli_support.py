from types import SimpleNamespace

import pytest
from eth_utils import to_checksum_address

from tidal.control_plane.client import ControlPlaneError
from tidal.control_plane.outbox import ActionReportOutbox
from tidal.operator_cli_support import BaseFeeCapSkip, broadcast_prepared_action
from tidal.transaction_service.types import TxIntent


class _FakeWeb3Client:
    def __init__(
        self,
        *,
        base_fee_wei: int = int(0.5 * 10**9),
        priority_fee_wei: int = int(1 * 10**9),
        base_fee_error: Exception | None = None,
    ) -> None:
        self.closed = False
        self.base_fee_wei = base_fee_wei
        self.priority_fee_wei = priority_fee_wei
        self.base_fee_error = base_fee_error

    async def get_transaction_count(self, _sender: str) -> int:
        return 7

    async def get_base_fee(self) -> int:
        if self.base_fee_error is not None:
            raise self.base_fee_error
        return self.base_fee_wei

    async def get_max_priority_fee(self) -> int:
        return self.priority_fee_wei

    async def send_raw_transaction(self, _signed_tx: bytes) -> str:
        return "0xabc"

    async def get_transaction_receipt(self, _tx_hash: str, *, timeout_seconds: int = 120) -> dict[str, object]:
        assert timeout_seconds == 120
        return {
            "status": 1,
            "blockNumber": 123,
            "gasUsed": 210000,
            "effectiveGasPrice": int(1.5 * 10**9),
        }

    async def close(self) -> None:
        self.closed = True


class _FakeClient:
    def __init__(self, *, base_url: str = "https://api.example.com", fail_broadcast: bool = False, fail_receipt: bool = False) -> None:
        self.base_url = base_url
        self.fail_broadcast = fail_broadcast
        self.fail_receipt = fail_receipt
        self.broadcast_reports: list[dict[str, object]] = []
        self.receipt_reports: list[dict[str, object]] = []

    def report_broadcast(self, _action_id: str, body: dict[str, object]) -> None:
        if self.fail_broadcast:
            raise ControlPlaneError("API returned 503: unavailable", status_code=503)
        self.broadcast_reports.append(body)

    def report_receipt(self, _action_id: str, body: dict[str, object]) -> None:
        if self.fail_receipt:
            raise ControlPlaneError("API returned 503: unavailable", status_code=503)
        self.receipt_reports.append(body)


class _CapturingSigner:
    def __init__(self) -> None:
        self.address = "0x9999999999999999999999999999999999999999"
        self.checksum_address = to_checksum_address(self.address)
        self.seen_txs: list[dict[str, object]] = []

    def sign_transaction(self, tx: dict[str, object]) -> bytes:
        self.seen_txs.append(tx)
        return b"signed"


def _kick_tx_intent(*, sender: str, to: str = "0x846475a1b97ac57861813206749c1b0f592383ef") -> TxIntent:
    return TxIntent(
        operation="kick",
        to=to,
        data="0xdeadbeef",
        value="0x0",
        chain_id=1,
        sender=sender,
        gas_estimate=210000,
        gas_limit=252000,
    )


@pytest.mark.asyncio
async def test_broadcast_prepared_action_checksums_to_and_closes_client(monkeypatch, tmp_path) -> None:
    web3_client = _FakeWeb3Client()
    client = _FakeClient()
    outbox = ActionReportOutbox(tmp_path / "action_outbox.db")
    signer = _CapturingSigner()
    sender = signer.address
    to_address = "0x846475a1b97ac57861813206749c1b0f592383ef"

    monkeypatch.setattr("tidal.operator_cli_support.build_web3_client", lambda settings: web3_client)

    records = await broadcast_prepared_action(
        settings=SimpleNamespace(
            txn_max_priority_fee_gwei=2,
            txn_base_fee_cap_gwei=5,
            chain_id=1,
        ),
        client=client,
        action_id="action-1",
        sender=sender,
        signer=signer,
        outbox=outbox,
        transactions=[_kick_tx_intent(sender=sender, to=to_address)],
    )

    assert signer.seen_txs
    assert signer.seen_txs[0]["to"] == to_checksum_address(to_address)
    assert signer.seen_txs[0]["maxFeePerGas"] == int(1.5 * 10**9)
    assert web3_client.closed is True
    assert records[0]["txHash"] == "0xabc"
    assert outbox.pending_count(base_url=client.base_url) == 0


@pytest.mark.asyncio
async def test_broadcast_prepared_action_queues_reports_for_retry(monkeypatch, tmp_path) -> None:
    web3_client = _FakeWeb3Client()
    client = _FakeClient(fail_broadcast=True, fail_receipt=True)
    outbox = ActionReportOutbox(tmp_path / "action_outbox.db")
    signer = _CapturingSigner()
    sender = signer.address

    monkeypatch.setattr("tidal.operator_cli_support.build_web3_client", lambda settings: web3_client)

    records = await broadcast_prepared_action(
        settings=SimpleNamespace(
            txn_max_priority_fee_gwei=2,
            txn_base_fee_cap_gwei=5,
            chain_id=1,
        ),
        client=client,
        action_id="action-1",
        sender=sender,
        signer=signer,
        outbox=outbox,
        transactions=[_kick_tx_intent(sender=sender)],
    )

    assert records[0]["txHash"] == "0xabc"
    assert outbox.pending_count(base_url=client.base_url) == 2

    recovery_client = _FakeClient(base_url=client.base_url)
    delivered = outbox.flush_pending(recovery_client)

    assert delivered == 2
    assert outbox.pending_count(base_url=client.base_url) == 0
    assert recovery_client.broadcast_reports[0]["txHash"] == "0xabc"
    assert recovery_client.receipt_reports[0]["receiptStatus"] == "CONFIRMED"


@pytest.mark.asyncio
async def test_broadcast_prepared_action_uses_base_fee_cap_for_max_fee(monkeypatch, tmp_path) -> None:
    web3_client = _FakeWeb3Client(base_fee_wei=int(3 * 10**9), priority_fee_wei=int(1 * 10**9))
    client = _FakeClient()
    outbox = ActionReportOutbox(tmp_path / "action_outbox.db")
    signer = _CapturingSigner()

    monkeypatch.setattr("tidal.operator_cli_support.build_web3_client", lambda settings: web3_client)

    await broadcast_prepared_action(
        settings=SimpleNamespace(
            txn_max_priority_fee_gwei=2,
            txn_base_fee_cap_gwei=5,
            chain_id=1,
        ),
        client=client,
        action_id="action-1",
        sender=signer.address,
        signer=signer,
        outbox=outbox,
        transactions=[_kick_tx_intent(sender=signer.address)],
        base_fee_cap_gwei=5,
    )

    assert signer.seen_txs[0]["maxFeePerGas"] == int(6 * 10**9)


@pytest.mark.asyncio
async def test_broadcast_prepared_action_skips_above_base_fee_cap(monkeypatch, tmp_path) -> None:
    web3_client = _FakeWeb3Client(base_fee_wei=int(6 * 10**9))
    client = _FakeClient()
    outbox = ActionReportOutbox(tmp_path / "action_outbox.db")
    signer = _CapturingSigner()

    monkeypatch.setattr("tidal.operator_cli_support.build_web3_client", lambda settings: web3_client)

    with pytest.raises(BaseFeeCapSkip, match="Base fee 6.00 gwei exceeds cap 5.00"):
        await broadcast_prepared_action(
            settings=SimpleNamespace(
                txn_max_priority_fee_gwei=2,
                txn_base_fee_cap_gwei=5,
                chain_id=1,
            ),
            client=client,
            action_id="action-1",
            sender=signer.address,
            signer=signer,
            outbox=outbox,
            transactions=[_kick_tx_intent(sender=signer.address)],
            base_fee_cap_gwei=5,
        )

    assert signer.seen_txs == []
    assert client.broadcast_reports == []
    assert web3_client.closed is True


@pytest.mark.asyncio
async def test_broadcast_prepared_action_skips_when_base_fee_check_fails(monkeypatch, tmp_path) -> None:
    web3_client = _FakeWeb3Client(base_fee_error=RuntimeError("rpc unavailable"))
    client = _FakeClient()
    outbox = ActionReportOutbox(tmp_path / "action_outbox.db")
    signer = _CapturingSigner()

    monkeypatch.setattr("tidal.operator_cli_support.build_web3_client", lambda settings: web3_client)

    with pytest.raises(BaseFeeCapSkip, match="Base fee check failed"):
        await broadcast_prepared_action(
            settings=SimpleNamespace(
                txn_max_priority_fee_gwei=2,
                txn_base_fee_cap_gwei=5,
                chain_id=1,
            ),
            client=client,
            action_id="action-1",
            sender=signer.address,
            signer=signer,
            outbox=outbox,
            transactions=[_kick_tx_intent(sender=signer.address)],
            base_fee_cap_gwei=5,
        )

    assert signer.seen_txs == []
    assert client.broadcast_reports == []
    assert web3_client.closed is True


@pytest.mark.asyncio
async def test_broadcast_prepared_action_emits_confirmation_progress(monkeypatch, tmp_path) -> None:
    web3_client = _FakeWeb3Client()
    client = _FakeClient()
    outbox = ActionReportOutbox(tmp_path / "action_outbox.db")
    signer = _CapturingSigner()
    sender = signer.address
    progress_messages: list[str] = []

    monkeypatch.setattr("tidal.operator_cli_support.build_web3_client", lambda settings: web3_client)

    await broadcast_prepared_action(
        settings=SimpleNamespace(
            txn_max_priority_fee_gwei=2,
            txn_base_fee_cap_gwei=5,
            chain_id=1,
        ),
        client=client,
        action_id="action-1",
        sender=sender,
        signer=signer,
        outbox=outbox,
        progress_callback=progress_messages.append,
        transactions=[_kick_tx_intent(sender=sender)],
    )

    assert progress_messages == ["Awaiting confirmation 0xabc...0xabc"]


@pytest.mark.asyncio
async def test_broadcast_prepared_action_requires_typed_intents() -> None:
    with pytest.raises(TypeError, match="TxIntent"):
        await broadcast_prepared_action(
            settings=SimpleNamespace(
                txn_max_priority_fee_gwei=2,
                txn_base_fee_cap_gwei=5,
                chain_id=1,
            ),
            client=_FakeClient(),
            action_id="action-1",
            sender="0x9999999999999999999999999999999999999999",
            signer=_CapturingSigner(),
            transactions=[{"operation": "kick"}],
        )


@pytest.mark.asyncio
async def test_broadcast_prepared_action_rejects_underestimated_gas_limit(monkeypatch) -> None:
    web3_built = False

    def build_web3_client(_settings):  # noqa: ANN001
        nonlocal web3_built
        web3_built = True
        return _FakeWeb3Client()

    monkeypatch.setattr("tidal.operator_cli_support.build_web3_client", build_web3_client)
    tx = _kick_tx_intent(sender="0x9999999999999999999999999999999999999999")
    tx.gas_estimate = 1_526_206
    tx.gas_limit = 500_000

    with pytest.raises(RuntimeError, match="gas limit 500,000 is below estimated gas 1,526,206"):
        await broadcast_prepared_action(
            settings=SimpleNamespace(
                txn_max_priority_fee_gwei=2,
                txn_base_fee_cap_gwei=5,
                chain_id=1,
            ),
            client=_FakeClient(),
            action_id="action-1",
            sender="0x9999999999999999999999999999999999999999",
            signer=_CapturingSigner(),
            transactions=[tx],
        )

    assert web3_built is False
