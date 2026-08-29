"""Transaction intent builders for kick operations."""

from __future__ import annotations

from eth_utils import to_checksum_address

from tidal.chain.contracts.abis import AUCTION_KICKER_ABI
from tidal.normalizers import normalize_address
from tidal.transaction_service.types import PreparedKick, PreparedResolveAuction, TxIntent


class KickTxBuilder:
    """Build calldata and unsigned intents for kick-related operations."""

    def __init__(
        self,
        *,
        web3_client,
        auction_kicker_address: str,
        chain_id: int,
    ) -> None:
        self.web3_client = web3_client
        self.auction_kicker_address = auction_kicker_address
        self.chain_id = chain_id

    def _kicker_contract(self) -> tuple[str, object]:
        address = to_checksum_address(self.auction_kicker_address)
        return address, self.web3_client.contract(address, AUCTION_KICKER_ABI)

    def build_single_kick_intent(self, prepared_kick: PreparedKick, *, sender: str | None) -> TxIntent:
        kicker_address, kicker_contract = self._kicker_contract()
        tx_data = kicker_contract.functions.kick(*self._kick_args(prepared_kick))._encode_transaction_data()
        return TxIntent(
            operation="kick",
            to=normalize_address(kicker_address),
            data=tx_data,
            value="0x0",
            chain_id=self.chain_id,
            sender=sender,
        )

    def build_batch_kick_intent(self, prepared_kicks: list[PreparedKick], *, sender: str | None) -> TxIntent:
        kicker_address, kicker_contract = self._kicker_contract()
        kick_tuples = [self._kick_args(prepared_kick) for prepared_kick in prepared_kicks]
        tx_data = kicker_contract.functions.batchKick(kick_tuples)._encode_transaction_data()
        return TxIntent(
            operation="kick",
            to=normalize_address(kicker_address),
            data=tx_data,
            value="0x0",
            chain_id=self.chain_id,
            sender=sender,
        )

    def build_resolve_auction_intent(
        self,
        prepared_operation: PreparedResolveAuction,
        *,
        sender: str | None,
    ) -> TxIntent:
        kicker_address, kicker_contract = self._kicker_contract()
        tx_data = kicker_contract.functions.resolveAuction(
            to_checksum_address(prepared_operation.candidate.auction_address),
            to_checksum_address(prepared_operation.sell_token),
            prepared_operation.requires_force,
        )._encode_transaction_data()
        return TxIntent(
            operation="resolve-auction",
            to=normalize_address(kicker_address),
            data=tx_data,
            value="0x0",
            chain_id=self.chain_id,
            sender=sender,
        )

    @staticmethod
    def _kick_args(prepared_kick: PreparedKick) -> tuple:
        return (
            to_checksum_address(prepared_kick.candidate.source_address),
            to_checksum_address(prepared_kick.candidate.auction_address),
            to_checksum_address(prepared_kick.candidate.token_address),
            prepared_kick.sell_amount,
            to_checksum_address(prepared_kick.candidate.want_address),
            prepared_kick.starting_price_raw,
            prepared_kick.minimum_price_scaled_1e18,
            prepared_kick.step_decay_rate_bps,
        )
