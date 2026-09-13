"""Unit/integration fixtures must never reach live RPC, pricing or delivery."""
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def prohibit_network_for_isolated_tests(request, monkeypatch):
    relative = Path(request.node.path).relative_to(Path(__file__).parent)
    if relative.parts[0] == "fork":
        return

    original = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("Live network access is prohibited in unit/integration tests")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def recording_execution():
    """Isolate caller preparation from the separately tested durable sender.

    Return a provisional inclusion, never fake a finalized business outcome.
    """
    def build(session):
        from tidal.persistence.repositories import KickTxRepository

        async def submit(*, transaction, operations, action, prepared_at_monotonic=None):
            del transaction, action, prepared_at_monotonic
            ids = [KickTxRepository(session).insert(row) for row in operations]
            session.commit()
            return {"status": "INCLUDED", "tx_hash": "0x" + "ab" * 32,
                    "operation_ids": ids, "block_number": 999}

        return SimpleNamespace(submit=AsyncMock(side_effect=submit))

    return build
