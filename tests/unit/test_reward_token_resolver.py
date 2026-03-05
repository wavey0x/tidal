import pytest

from tidal.constants import CORE_REWARD_TOKENS
from tidal.scanner.reward_token_resolver import RewardTokenResolver


class FakeStrategyRewardsReader:
    async def rewards_tokens(self, strategy_address: str) -> list[str]:
        del strategy_address
        return ["0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"]

    async def rewards_tokens_many(self, strategy_addresses: list[str]) -> tuple[dict[str, list[str] | None], dict[str, int]]:
        return (
            {address: ["0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"] for address in strategy_addresses},
            {
                "batch_count": 1,
                "subcalls_total": len(strategy_addresses),
                "subcalls_failed": 0,
                "fallback_direct_calls_total": 0,
            },
        )


@pytest.mark.asyncio
async def test_resolver_includes_core_and_extra_tokens() -> None:
    resolver = RewardTokenResolver(FakeStrategyRewardsReader())
    result = await resolver.resolve("0x0000000000000000000000000000000000000001")

    assert CORE_REWARD_TOKENS.issubset(result)
    assert "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48" in result


@pytest.mark.asyncio
async def test_resolve_many_includes_core_and_extra_tokens() -> None:
    resolver = RewardTokenResolver(FakeStrategyRewardsReader())
    results, stats = await resolver.resolve_many(
        [
            "0x0000000000000000000000000000000000000001",
            "0x0000000000000000000000000000000000000002",
        ]
    )

    assert stats["subcalls_total"] == 2
    assert CORE_REWARD_TOKENS.issubset(results["0x0000000000000000000000000000000000000001"])
    assert "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48" in results[
        "0x0000000000000000000000000000000000000002"
    ]
