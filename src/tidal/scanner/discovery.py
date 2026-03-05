"""Strategy discovery services."""

from __future__ import annotations

from tidal.chain.contracts.yearn import YearnCurveFactoryReader
from tidal.constants import ADDITIONAL_DISCOVERY_STRATEGIES, ADDITIONAL_DISCOVERY_VAULTS
from tidal.types import DiscoveredStrategy


class StrategyDiscoveryService:
    """Discovers strategies by traversing vault withdrawal queues."""

    def __init__(self, yearn_reader: YearnCurveFactoryReader, concurrency: int = 20):
        del concurrency
        self.yearn_reader = yearn_reader

    async def discover(self) -> tuple[list[DiscoveredStrategy], int, dict[str, int]]:
        factory_vaults = await self.yearn_reader.all_deployed_vaults()
        vaults = sorted(set(factory_vaults).union(ADDITIONAL_DISCOVERY_VAULTS))
        mapping, stage_stats = await self.yearn_reader.strategies_for_vaults_batched(vaults)

        for strategy_address in ADDITIONAL_DISCOVERY_STRATEGIES:
            if any(strategy_address in strategies for strategies in mapping.values()):
                continue
            try:
                vault_address = await self.yearn_reader.vault_for_strategy(strategy_address)
            except Exception:  # noqa: BLE001
                continue
            mapping.setdefault(vault_address, []).append(strategy_address)

        discovered: list[DiscoveredStrategy] = []
        for vault, strategies in mapping.items():
            for strategy in strategies:
                discovered.append(
                    DiscoveredStrategy(strategy_address=strategy, vault_address=vault)
                )

        vault_count = len(set(vaults).union(mapping.keys()))
        return discovered, vault_count, stage_stats
