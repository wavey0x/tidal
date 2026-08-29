"""Shared readers for auction contract state."""

from __future__ import annotations

from collections.abc import Callable

from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address
from hexbytes import HexBytes

from tidal.chain.contracts.abis import AUCTION_VERSION_ABI, SUPPORTED_AUCTION_ABI
from tidal.chain.contracts.multicall import MulticallClient, MulticallRequest
from tidal.normalizers import normalize_address


class AuctionStateReader:
    """Reads auction contract state through multicall or direct RPC fallbacks."""

    def __init__(
        self,
        *,
        web3_client,
        multicall_client: MulticallClient | None,
        multicall_enabled: bool,
        multicall_auction_batch_calls: int,
    ) -> None:
        self.web3_client = web3_client
        self.multicall_client = multicall_client
        self.multicall_enabled = multicall_enabled
        self.multicall_auction_batch_calls = multicall_auction_batch_calls

    async def read_bool_noargs_many(self, auction_addresses: list[str], method_name: str) -> dict[str, bool | None]:
        return await self._read_noarg_many(
            auction_addresses,
            method_name,
            direct_transform=bool,
            decoder=lambda data: bool(abi_decode(["bool"], data)[0]),
        )

    async def read_address_array_noargs_many(
        self,
        auction_addresses: list[str],
        method_name: str,
    ) -> dict[str, list[str] | None]:
        return await self._read_noarg_many(
            auction_addresses,
            method_name,
            direct_transform=lambda value: [normalize_address(item) for item in value],
            decoder=lambda data: [normalize_address(item) for item in abi_decode(["address[]"], data)[0]],
        )

    async def read_address_noargs_many(self, auction_addresses: list[str], method_name: str) -> dict[str, str | None]:
        return await self._read_noarg_many(
            auction_addresses,
            method_name,
            direct_transform=lambda value: normalize_address(value),
            decoder=lambda data: normalize_address(abi_decode(["address"], data)[0]),
        )

    async def read_uint_noargs_many(self, auction_addresses: list[str], method_name: str) -> dict[str, int | None]:
        return await self._read_noarg_many(
            auction_addresses,
            method_name,
            direct_transform=int,
            decoder=lambda data: int(abi_decode(["uint256"], data)[0]),
        )

    async def read_string_noargs_many(
        self,
        auction_addresses: list[str],
        method_name: str,
    ) -> dict[str, str | None]:
        return await self._read_noarg_many(
            auction_addresses,
            method_name,
            direct_transform=lambda value: str(value).strip(),
            decoder=lambda data: str(abi_decode(["string"], data)[0]).strip(),
            abi=AUCTION_VERSION_ABI if method_name == "version" else SUPPORTED_AUCTION_ABI,
        )

    async def read_bool_arg_many(self, pairs: list[tuple[str, str]], method_name: str) -> dict[tuple[str, str], bool | None]:
        return await self._read_arg_many(
            pairs,
            method_name,
            direct_transform=bool,
            decoder=lambda data: bool(abi_decode(["bool"], data)[0]),
        )

    async def read_uint_arg_many(self, pairs: list[tuple[str, str]], method_name: str) -> dict[tuple[str, str], int | None]:
        return await self._read_arg_many(
            pairs,
            method_name,
            direct_transform=int,
            decoder=lambda data: int(abi_decode(["uint256"], data)[0]),
        )

    async def read_auction_token_enabled_many(
        self,
        pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], bool | None]:
        return await self._read_arg_many(
            pairs,
            "auctions",
            direct_transform=lambda value: int(value[1]) != 0,
            decoder=lambda data: int(abi_decode(["uint64", "uint64", "uint128"], data)[1]) != 0,
        )

    async def _read_noarg_many(
        self,
        auction_addresses: list[str],
        method_name: str,
        *,
        direct_transform: Callable[[object], object],
        decoder: Callable[[bytes], object],
        abi: list[dict] = SUPPORTED_AUCTION_ABI,
    ) -> dict[str, object | None]:
        output: dict[str, object | None] = {auction: None for auction in auction_addresses}
        if not auction_addresses:
            return output

        if not self.multicall_enabled or self.multicall_client is None:
            for auction_address in auction_addresses:
                contract = self.web3_client.contract(auction_address, abi)
                fn = getattr(contract.functions, method_name)()
                try:
                    output[auction_address] = direct_transform(await self.web3_client.call(fn))
                except Exception:  # noqa: BLE001
                    output[auction_address] = None
            return output

        requests: list[MulticallRequest] = []
        for auction_address in auction_addresses:
            contract = self.web3_client.contract(auction_address, abi)
            fn = getattr(contract.functions, method_name)()
            requests.append(
                MulticallRequest(
                    target=auction_address,
                    call_data=bytes(HexBytes(fn._encode_transaction_data())),
                    logical_key=(auction_address, method_name),
                )
            )

        results = await self.multicall_client.execute(
            requests,
            batch_size=self.multicall_auction_batch_calls,
            allow_failure=True,
        )
        for result in results:
            auction_address = result.logical_key[0]
            if not result.success:
                continue
            try:
                output[auction_address] = decoder(result.return_data)
            except Exception:  # noqa: BLE001
                output[auction_address] = None
        return output

    async def _read_arg_many(
        self,
        pairs: list[tuple[str, str]],
        method_name: str,
        *,
        direct_transform: Callable[[object], object],
        decoder: Callable[[bytes], object],
    ) -> dict[tuple[str, str], object | None]:
        output: dict[tuple[str, str], object | None] = {pair: None for pair in pairs}
        if not pairs:
            return output

        if not self.multicall_enabled or self.multicall_client is None:
            for auction_address, token_address in pairs:
                contract = self.web3_client.contract(auction_address, SUPPORTED_AUCTION_ABI)
                fn = getattr(contract.functions, method_name)(to_checksum_address(token_address))
                try:
                    output[(auction_address, token_address)] = direct_transform(await self.web3_client.call(fn))
                except Exception:  # noqa: BLE001
                    output[(auction_address, token_address)] = None
            return output

        requests: list[MulticallRequest] = []
        for auction_address, token_address in pairs:
            contract = self.web3_client.contract(auction_address, SUPPORTED_AUCTION_ABI)
            fn = getattr(contract.functions, method_name)(to_checksum_address(token_address))
            requests.append(
                MulticallRequest(
                    target=auction_address,
                    call_data=bytes(HexBytes(fn._encode_transaction_data())),
                    logical_key=(auction_address, token_address, method_name),
                )
            )

        results = await self.multicall_client.execute(
            requests,
            batch_size=self.multicall_auction_batch_calls,
            allow_failure=True,
        )
        for result in results:
            auction_address = result.logical_key[0]
            token_address = result.logical_key[1]
            if not result.success:
                continue
            try:
                output[(auction_address, token_address)] = decoder(result.return_data)
            except Exception:  # noqa: BLE001
                output[(auction_address, token_address)] = None
        return output
