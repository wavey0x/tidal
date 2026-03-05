"""Token metadata caching service."""

from __future__ import annotations

from tidal.chain.contracts.erc20 import ERC20Reader
from tidal.normalizers import normalize_address
from tidal.time import utcnow_iso
from tidal.types import TokenMetadata


class TokenMetadataService:
    """Fetches token metadata on first sight and reuses cached values afterwards."""

    def __init__(self, chain_id: int, token_repository, erc20_reader: ERC20Reader):
        self.chain_id = chain_id
        self.token_repository = token_repository
        self.erc20_reader = erc20_reader

    async def get_or_fetch(self, token_address: str, *, is_core_reward: bool) -> TokenMetadata:
        token_address = normalize_address(token_address)
        existing = self.token_repository.get(token_address)
        if existing is not None:
            return existing

        now_iso = utcnow_iso()
        decimals = await self.erc20_reader.read_decimals(token_address)
        symbol = await self.erc20_reader.read_symbol(token_address)
        name = await self.erc20_reader.read_name(token_address)

        metadata = TokenMetadata(
            address=token_address,
            chain_id=self.chain_id,
            name=name,
            symbol=symbol,
            decimals=decimals,
            is_core_reward=is_core_reward,
            first_seen_at=now_iso,
            last_seen_at=now_iso,
        )
        self.token_repository.upsert(metadata)
        return metadata
