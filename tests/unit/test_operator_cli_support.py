from types import SimpleNamespace

import pytest
from eth_utils import to_checksum_address

from tidal.control_plane.client import ControlPlaneError
from tidal.control_plane.outbox import ActionReportOutbox
from tidal.operator_cli_support import broadcast_prepared_action


class _FakeWeb3Client:
    def __init__(self) -> None:
        self.closed = False

    async def get_transaction_count(self, _sender: str) -> int:
        return 7

    async def get_base_fee(self) -> int:
        return int(0.5 * 10**9)

    async def get_max_priority_fee(self) -> int:
        return int(1 * 10**9)

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
            txn_max_base_fee_gwei=0.5,
            chain_id=1,
        ),
        client=client,
        action_id="action-1",
        sender=sender,
        signer=signer,
        outbox=outbox,
        transactions=[
            {
                "operation": "kick",
                "to": to_address,
                "data": "0xdeadbeef",
                "value": "0x0",
                "chainId": 1,
                "sender": sender,
                "gasEstimate": 210000,
                "gasLimit": 252000,
            }
        ],
    )

    assert signer.seen_txs
    assert signer.seen_txs[0]["to"] == to_checksum_address(to_address)
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
            txn_max_base_fee_gwei=0.5,
            chain_id=1,
        ),
        client=client,
        action_id="action-1",
        sender=sender,
        signer=signer,
        outbox=outbox,
        transactions=[
            {
                "operation": "kick",
                "to": "0x846475a1b97ac57861813206749c1b0f592383ef",
                "data": "0xdeadbeef",
                "value": "0x0",
                "chainId": 1,
                "sender": sender,
                "gasEstimate": 210000,
                "gasLimit": 252000,
            }
        ],
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
            txn_max_base_fee_gwei=0.5,
            chain_id=1,
        ),
        client=client,
        action_id="action-1",
        sender=sender,
        signer=signer,
        outbox=outbox,
        progress_callback=progress_messages.append,
        transactions=[
            {
                "operation": "kick",
                "to": "0x846475a1b97ac57861813206749c1b0f592383ef",
                "data": "0xdeadbeef",
                "value": "0x0",
                "chainId": 1,
                "sender": sender,
                "gasEstimate": 210000,
                "gasLimit": 252000,
            }
        ],
    )

    assert progress_messages == ["Awaiting confirmation 0xabc...0xabc"]
