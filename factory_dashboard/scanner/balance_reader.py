"""Balance reading service."""

from __future__ import annotations

from factory_dashboard.chain.contracts.erc20 import ERC20Reader
from factory_dashboard.types import BalancePair


class BalanceReader:
    """Reads token balances for source-token pairs."""

    def __init__(self, erc20_reader: ERC20Reader):
        self.erc20_reader = erc20_reader

    async def read(self, source_address: str, token_address: str) -> int:
        return await self.erc20_reader.read_balance(token_address, source_address)

    async def read_many(self, pairs: list[BalancePair]) -> tuple[dict[BalancePair, int | None], dict[str, int]]:
        raw_pairs = [(pair.source_address, pair.token_address) for pair in pairs]
        results, stats = await self.erc20_reader.read_balances_many(raw_pairs)

        mapped: dict[BalancePair, int | None] = {}
        for pair in pairs:
            key = (pair.source_address, pair.token_address)
            mapped[pair] = results.get(key)

        return mapped, stats
