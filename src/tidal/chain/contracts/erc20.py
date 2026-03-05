"""ERC20 token contract reader."""

from __future__ import annotations

from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address
from hexbytes import HexBytes

from tidal.chain.contracts.abis import ERC20_ABI
from tidal.chain.contracts.multicall import MulticallClient, MulticallRequest
from tidal.chain.web3_client import Web3Client
from tidal.normalizers import normalize_address


class ERC20Reader:
    """Reads metadata and balances from ERC20 contracts."""

    def __init__(
        self,
        web3_client: Web3Client,
        *,
        multicall_client: MulticallClient | None = None,
        multicall_enabled: bool = True,
        multicall_balance_batch_calls: int = 1000,
    ):
        self.web3_client = web3_client
        self.multicall_client = multicall_client
        self.multicall_enabled = multicall_enabled
        self.multicall_balance_batch_calls = multicall_balance_batch_calls

    async def read_name(self, token_address: str) -> str | None:
        contract = self.web3_client.contract(token_address, ERC20_ABI)
        try:
            value = await self.web3_client.call(contract.functions.name())
            return str(value)
        except Exception:  # noqa: BLE001
            return None

    async def read_symbol(self, token_address: str) -> str | None:
        contract = self.web3_client.contract(token_address, ERC20_ABI)
        try:
            value = await self.web3_client.call(contract.functions.symbol())
            return str(value)
        except Exception:  # noqa: BLE001
            return None

    async def read_decimals(self, token_address: str) -> int:
        contract = self.web3_client.contract(token_address, ERC20_ABI)
        value = await self.web3_client.call(contract.functions.decimals())
        return int(value)

    async def read_balance(self, token_address: str, holder_address: str) -> int:
        token_address = normalize_address(token_address)
        holder_address = normalize_address(holder_address)
        contract = self.web3_client.contract(token_address, ERC20_ABI)
        value = await self.web3_client.call(contract.functions.balanceOf(to_checksum_address(holder_address)))
        return int(value)

    async def read_balances_many(
        self,
        pairs: list[tuple[str, str]],
    ) -> tuple[dict[tuple[str, str], int | None], dict[str, int]]:
        """Batch-read balanceOf(strategy) for (strategy, token) pairs."""

        normalized_pairs = [
            (normalize_address(strategy), normalize_address(token))
            for strategy, token in pairs
        ]
        output: dict[tuple[str, str], int | None] = {pair: None for pair in normalized_pairs}
        stats = {
            "batch_count": 0,
            "subcalls_total": 0,
            "subcalls_failed": 0,
            "fallback_direct_calls_total": 0,
        }

        if not normalized_pairs:
            return output, stats

        if not self.multicall_enabled or self.multicall_client is None:
            for strategy_address, token_address in normalized_pairs:
                try:
                    output[(strategy_address, token_address)] = await self.read_balance(token_address, strategy_address)
                except Exception:  # noqa: BLE001
                    output[(strategy_address, token_address)] = None
            return output, stats

        requests: list[MulticallRequest] = []
        for strategy_address, token_address in normalized_pairs:
            token_contract = self.web3_client.contract(token_address, ERC20_ABI)
            fn = token_contract.functions.balanceOf(to_checksum_address(strategy_address))
            requests.append(
                MulticallRequest(
                    target=token_address,
                    call_data=bytes(HexBytes(fn._encode_transaction_data())),
                    logical_key=(strategy_address, token_address),
                )
            )

        multicall_results = await self.multicall_client.execute(
            requests,
            batch_size=self.multicall_balance_batch_calls,
            allow_failure=True,
        )
        stage_stats = self.multicall_client.last_stats
        stats["batch_count"] += stage_stats.batch_count
        stats["subcalls_total"] += stage_stats.subcalls_total
        stats["subcalls_failed"] += stage_stats.subcalls_failed
        stats["fallback_direct_calls_total"] += stage_stats.fallback_direct_calls_total

        for result in multicall_results:
            strategy_address = result.logical_key[0]
            token_address = result.logical_key[1]
            key = (strategy_address, token_address)

            if not result.success:
                output[key] = None
                continue

            try:
                value = int(abi_decode(["uint256"], result.return_data)[0])
                output[key] = value
            except Exception:  # noqa: BLE001
                stats["subcalls_failed"] += 1
                output[key] = None

        return output, stats
