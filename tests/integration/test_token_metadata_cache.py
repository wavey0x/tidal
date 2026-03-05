import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tidal.persistence import models
from tidal.persistence.repositories import TokenRepository
from tidal.scanner.token_metadata import TokenMetadataService


class FakeERC20Reader:
    def __init__(self) -> None:
        self.decimals_calls = 0
        self.symbol_calls = 0
        self.name_calls = 0

    async def read_decimals(self, token_address: str) -> int:
        del token_address
        self.decimals_calls += 1
        return 18

    async def read_symbol(self, token_address: str) -> str:
        del token_address
        self.symbol_calls += 1
        return "TEST"

    async def read_name(self, token_address: str) -> str:
        del token_address
        self.name_calls += 1
        return "Test Token"


@pytest.mark.asyncio
async def test_get_or_fetch_reads_once_then_uses_cache() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        repo = TokenRepository(session)
        reader = FakeERC20Reader()
        svc = TokenMetadataService(chain_id=1, token_repository=repo, erc20_reader=reader)

        token = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        _ = await svc.get_or_fetch(token, is_core_reward=False)
        _ = await svc.get_or_fetch(token, is_core_reward=False)

        assert reader.decimals_calls == 1
        assert reader.symbol_calls == 1
        assert reader.name_calls == 1
