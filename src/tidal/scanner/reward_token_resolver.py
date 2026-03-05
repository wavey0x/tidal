"""Resolve reward token sets for strategies."""

from __future__ import annotations

from tidal.chain.contracts.yearn import StrategyRewardsReader
from tidal.constants import CORE_REWARD_TOKENS
from tidal.normalizers import normalize_address


class RewardTokenResolver:
    """Resolve rewards token addresses for one or many strategies."""

    def __init__(self, strategy_rewards_reader: StrategyRewardsReader):
        self.strategy_rewards_reader = strategy_rewards_reader

    async def resolve(self, strategy_address: str) -> set[str]:
        extra_tokens = await self.strategy_rewards_reader.rewards_tokens(strategy_address)
        normalized_extra = {normalize_address(token) for token in extra_tokens}
        return set(CORE_REWARD_TOKENS).union(normalized_extra)

    async def resolve_many(self, strategy_addresses: list[str]) -> tuple[dict[str, set[str]], dict[str, int]]:
        """Resolve rewards for many strategies with multicall-backed reader when available."""

        normalized = [normalize_address(address) for address in strategy_addresses]
        token_rows, stats = await self.strategy_rewards_reader.rewards_tokens_many(normalized)

        resolved: dict[str, set[str]] = {}
        for strategy_address in normalized:
            extras = token_rows.get(strategy_address, [])
            resolved[strategy_address] = set(CORE_REWARD_TOKENS).union(
                {normalize_address(token) for token in extras}
            )

        return resolved, stats
