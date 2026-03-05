import pytest

from tidal.constants import ADDITIONAL_DISCOVERY_VAULTS
from tidal.scanner.discovery import StrategyDiscoveryService


class FakeYearnReader:
    def __init__(self, factory_vaults: list[str]) -> None:
        self.factory_vaults = factory_vaults
        self.received_vaults: list[str] = []

    async def all_deployed_vaults(self) -> list[str]:
        return self.factory_vaults

    async def strategies_for_vaults_batched(self, vault_addresses: list[str]):
        self.received_vaults = list(vault_addresses)
        forced_vault = next(iter(ADDITIONAL_DISCOVERY_VAULTS))
        return (
            {
                "0x1111111111111111111111111111111111111111": [
                    "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ],
                forced_vault: [
                    "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                ],
            },
            {
                "batch_count": 1,
                "subcalls_total": 8,
                "subcalls_failed": 0,
                "fallback_direct_calls_total": 0,
                "overflow_vaults_count": 0,
            },
        )

    async def vault_for_strategy(self, strategy_address: str) -> str:
        del strategy_address
        return "0x0000000000000000000000000000000000000000"


@pytest.mark.asyncio
async def test_discovery_includes_hardcoded_vault_when_missing_from_factory() -> None:
    forced_vault = next(iter(ADDITIONAL_DISCOVERY_VAULTS))
    reader = FakeYearnReader(
        factory_vaults=[
            "0x1111111111111111111111111111111111111111",
        ]
    )
    service = StrategyDiscoveryService(reader)

    discovered, vaults_seen, _stats = await service.discover()

    assert sorted(reader.received_vaults) == sorted(
        [
            "0x1111111111111111111111111111111111111111",
            forced_vault,
        ]
    )
    assert vaults_seen == 2
    assert {item.vault_address for item in discovered} == {
        "0x1111111111111111111111111111111111111111",
        forced_vault,
    }


@pytest.mark.asyncio
async def test_discovery_dedupes_hardcoded_vault_if_factory_already_returns_it() -> None:
    forced_vault = next(iter(ADDITIONAL_DISCOVERY_VAULTS))
    reader = FakeYearnReader(
        factory_vaults=[
            "0x1111111111111111111111111111111111111111",
            forced_vault,
            forced_vault,
        ]
    )
    service = StrategyDiscoveryService(reader)

    _discovered, vaults_seen, _stats = await service.discover()

    assert len(reader.received_vaults) == 2
    assert vaults_seen == 2
