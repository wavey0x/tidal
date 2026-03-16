"""Yearn-specific contract readers."""

from __future__ import annotations

from eth_abi import decode as abi_decode
from hexbytes import HexBytes

from factory_dashboard.chain.contracts.abis import FACTORY_ABI, STRATEGY_ABI, VAULT_ABI
from factory_dashboard.chain.contracts.multicall import MulticallClient, MulticallRequest
from factory_dashboard.chain.web3_client import Web3Client
from factory_dashboard.constants import DEFAULT_MAX_WITHDRAWAL_QUEUE, ZERO_ADDRESS
from factory_dashboard.normalizers import normalize_address


class YearnCurveFactoryReader:
    """Reads vaults and strategies from Yearn's curve factory topology."""

    def __init__(
        self,
        web3_client: Web3Client,
        factory_address: str,
        *,
        multicall_client: MulticallClient | None = None,
        multicall_enabled: bool = True,
        multicall_discovery_batch_calls: int = 800,
        multicall_overflow_queue_max: int = 32,
    ):
        self.web3_client = web3_client
        self.factory_address = normalize_address(factory_address)
        self.multicall_client = multicall_client
        self.multicall_enabled = multicall_enabled
        self.multicall_discovery_batch_calls = multicall_discovery_batch_calls
        self.multicall_overflow_queue_max = multicall_overflow_queue_max

    async def all_deployed_vaults(self) -> list[str]:
        contract = self.web3_client.contract(self.factory_address, FACTORY_ABI)

        try:
            result = await self.web3_client.call(contract.functions.allDeployedVautls())
        except Exception:  # noqa: BLE001
            result = await self.web3_client.call(contract.functions.allDeployedVaults())

        return [normalize_address(addr) for addr in result]

    async def strategies_for_vault(self, vault_address: str) -> list[str]:
        contract = self.web3_client.contract(vault_address, VAULT_ABI)
        discovered: list[str] = []

        for index in range(DEFAULT_MAX_WITHDRAWAL_QUEUE):
            try:
                strategy = await self.web3_client.call(contract.functions.withdrawalQueue(index))
            except Exception:  # noqa: BLE001
                # Some vault implementations revert on out-of-bounds queue reads.
                break
            strategy_address = normalize_address(strategy)
            if strategy_address == ZERO_ADDRESS:
                break
            discovered.append(strategy_address)

        return discovered

    async def vault_for_strategy(self, strategy_address: str) -> str:
        contract = self.web3_client.contract(strategy_address, STRATEGY_ABI)
        vault_address = await self.web3_client.call(contract.functions.vault())
        return normalize_address(vault_address)

    async def strategies_for_vaults_batched(self, vault_addresses: list[str]) -> tuple[dict[str, list[str]], dict[str, int]]:
        """Batch-read withdrawal queues [0..3], with overflow fallback for index 3 hits."""

        normalized_vaults = [normalize_address(address) for address in vault_addresses]
        mapping: dict[str, list[str]] = {vault: [] for vault in normalized_vaults}
        stats = {
            "batch_count": 0,
            "subcalls_total": 0,
            "subcalls_failed": 0,
            "fallback_direct_calls_total": 0,
            "overflow_vaults_count": 0,
        }

        if not normalized_vaults:
            return mapping, stats

        if not self.multicall_enabled or self.multicall_client is None:
            for vault in normalized_vaults:
                mapping[vault] = await self.strategies_for_vault(vault)
            return mapping, stats

        requests: list[MulticallRequest] = []
        for vault in normalized_vaults:
            vault_contract = self.web3_client.contract(vault, VAULT_ABI)
            for index in range(4):
                fn = vault_contract.functions.withdrawalQueue(index)
                requests.append(
                    MulticallRequest(
                        target=vault,
                        call_data=bytes(HexBytes(fn._encode_transaction_data())),
                        logical_key=(vault, str(index)),
                    )
                )

        multicall_results = await self.multicall_client.execute(
            requests,
            batch_size=self.multicall_discovery_batch_calls,
            allow_failure=True,
        )

        stage_stats = self.multicall_client.last_stats
        stats["batch_count"] += stage_stats.batch_count
        stats["subcalls_total"] += stage_stats.subcalls_total
        stats["subcalls_failed"] += stage_stats.subcalls_failed
        stats["fallback_direct_calls_total"] += stage_stats.fallback_direct_calls_total

        result_by_key = {result.logical_key: result for result in multicall_results}
        overflow_candidates: list[str] = []

        for vault in normalized_vaults:
            strategies: list[str] = []
            index3_populated = False

            for index in range(4):
                result = result_by_key[(vault, str(index))]
                if not result.success:
                    break

                try:
                    strategy_address = normalize_address(abi_decode(["address"], result.return_data)[0])
                except Exception:  # noqa: BLE001
                    stats["subcalls_failed"] += 1
                    break

                if strategy_address == ZERO_ADDRESS:
                    break

                strategies.append(strategy_address)
                if index == 3:
                    index3_populated = True

            mapping[vault] = strategies
            if index3_populated:
                overflow_candidates.append(vault)

        for vault in overflow_candidates:
            vault_contract = self.web3_client.contract(vault, VAULT_ABI)
            for index in range(4, self.multicall_overflow_queue_max + 1):
                stats["fallback_direct_calls_total"] += 1
                try:
                    strategy = await self.web3_client.call(vault_contract.functions.withdrawalQueue(index))
                except Exception:  # noqa: BLE001
                    break

                strategy_address = normalize_address(strategy)
                if strategy_address == ZERO_ADDRESS:
                    break

                mapping[vault].append(strategy_address)

        stats["overflow_vaults_count"] = len(overflow_candidates)
        for vault in normalized_vaults:
            mapping[vault] = list(dict.fromkeys(mapping[vault]))

        return mapping, stats


class YearnNameReader:
    """Reads vault/strategy display names where supported."""

    def __init__(self, web3_client: Web3Client):
        self.web3_client = web3_client

    async def read_vault_name(self, vault_address: str) -> str | None:
        contract = self.web3_client.contract(vault_address, VAULT_ABI)
        value = await self.web3_client.call(contract.functions.name())
        if value is None:
            return None
        return str(value).strip() or None

    async def read_vault_symbol(self, vault_address: str) -> str | None:
        contract = self.web3_client.contract(vault_address, VAULT_ABI)
        value = await self.web3_client.call(contract.functions.symbol())
        if value is None:
            return None
        return str(value).strip() or None

    async def read_vault_deposit_limit(self, vault_address: str) -> str | None:
        contract = self.web3_client.contract(vault_address, VAULT_ABI)
        value = await self.web3_client.call(contract.functions.depositLimit())
        if value is None:
            return None
        return str(value)

    async def read_strategy_name(self, strategy_address: str) -> str | None:
        contract = self.web3_client.contract(strategy_address, STRATEGY_ABI)
        value = await self.web3_client.call(contract.functions.name())
        if value is None:
            return None
        return str(value).strip() or None


class StrategyRewardsReader:
    """Reads rewards token references from strategy contracts."""

    def __init__(
        self,
        web3_client: Web3Client,
        *,
        multicall_client: MulticallClient | None = None,
        multicall_enabled: bool = True,
        multicall_rewards_batch_calls: int = 500,
        multicall_rewards_index_max: int = 16,
    ):
        self.web3_client = web3_client
        self.multicall_client = multicall_client
        self.multicall_enabled = multicall_enabled
        self.multicall_rewards_batch_calls = multicall_rewards_batch_calls
        self.multicall_rewards_index_max = multicall_rewards_index_max

    async def rewards_tokens(self, strategy_address: str) -> list[str]:
        contract = self.web3_client.contract(strategy_address, STRATEGY_ABI)
        return await self._read_indexed_rewards_tokens(contract)

    async def rewards_tokens_many(self, strategy_addresses: list[str]) -> tuple[dict[str, list[str]], dict[str, int]]:
        """Batch-read indexed rewardsTokens(i), where first subcall failure terminates per strategy."""

        normalized_strategies = [normalize_address(address) for address in strategy_addresses]
        output: dict[str, list[str]] = {address: [] for address in normalized_strategies}
        stats = {
            "batch_count": 0,
            "subcalls_total": 0,
            "subcalls_failed": 0,
            "fallback_direct_calls_total": 0,
        }

        if not normalized_strategies:
            return output, stats

        if not self.multicall_enabled or self.multicall_client is None:
            for address in normalized_strategies:
                output[address] = await self.rewards_tokens(address)
            return output, stats

        requests: list[MulticallRequest] = []
        for address in normalized_strategies:
            strategy_contract = self.web3_client.contract(address, STRATEGY_ABI)
            for index in range(self.multicall_rewards_index_max):
                fn = strategy_contract.get_function_by_signature("rewardsTokens(uint256)")(index)
                requests.append(
                    MulticallRequest(
                        target=address,
                        call_data=bytes(HexBytes(fn._encode_transaction_data())),
                        logical_key=(address, str(index)),
                    )
                )

        multicall_results = await self.multicall_client.execute(
            requests,
            batch_size=self.multicall_rewards_batch_calls,
            allow_failure=True,
        )
        stage_stats = self.multicall_client.last_stats
        stats["batch_count"] += stage_stats.batch_count
        stats["subcalls_total"] += stage_stats.subcalls_total
        stats["subcalls_failed"] += stage_stats.subcalls_failed
        stats["fallback_direct_calls_total"] += stage_stats.fallback_direct_calls_total

        result_by_key = {result.logical_key: result for result in multicall_results}
        for strategy_address in normalized_strategies:
            tokens: list[str] = []
            for index in range(self.multicall_rewards_index_max):
                result = result_by_key[(strategy_address, str(index))]
                if not result.success:
                    # Failure is the normal termination signal for indexed reward arrays.
                    break

                try:
                    token_address = normalize_address(abi_decode(["address"], result.return_data)[0])
                except Exception:  # noqa: BLE001
                    stats["subcalls_failed"] += 1
                    break

                if token_address == ZERO_ADDRESS:
                    break
                tokens.append(token_address)

            output[strategy_address] = list(dict.fromkeys(tokens))

        return output, stats

    async def _read_indexed_rewards_tokens(self, strategy_contract) -> list[str]:
        tokens: list[str] = []
        for index in range(self.multicall_rewards_index_max):
            try:
                fn = strategy_contract.get_function_by_signature("rewardsTokens(uint256)")(index)
                token = await self.web3_client.call(fn)
            except Exception:  # noqa: BLE001
                # Out-of-bounds / unsupported index access is expected termination.
                break

            token_address = normalize_address(token)
            if token_address == ZERO_ADDRESS:
                break
            tokens.append(token_address)

        return list(dict.fromkeys(tokens))
