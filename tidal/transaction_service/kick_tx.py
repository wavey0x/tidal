"""Transaction intent builders for kick operations."""

from __future__ import annotations

from eth_utils import to_checksum_address

from tidal.chain.contracts.abis import AUCTION_KICKER_ABI
from tidal.normalizers import normalize_address
from tidal.transaction_service.types import KickRecoveryPlan, PreparedKick, PreparedSweepAndSettle, TxIntent


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
        if prepared_kick.recovery_plan is None:
            tx_data = kicker_contract.functions.kick(*self._kick_args(prepared_kick))._encode_transaction_data()
        else:
            tx_data = kicker_contract.functions.kickExtended(*self._kick_extended_args(prepared_kick))._encode_transaction_data()
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

    def build_sweep_and_settle_intent(
        self,
        prepared_operation: PreparedSweepAndSettle,
        *,
        sender: str | None,
    ) -> TxIntent:
        kicker_address, kicker_contract = self._kicker_contract()
        tx_data = kicker_contract.functions.sweepAndSettle(
            to_checksum_address(prepared_operation.candidate.auction_address),
            to_checksum_address(prepared_operation.sell_token),
        )._encode_transaction_data()
        return TxIntent(
            operation="sweep-and-settle",
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
            prepared_kick.starting_price_unscaled,
            prepared_kick.minimum_price_scaled_1e18,
            prepared_kick.step_decay_rate_bps,
            (
                to_checksum_address(prepared_kick.settle_token)
                if prepared_kick.settle_token
                else "0x0000000000000000000000000000000000000000"
            ),
        )

    @staticmethod
    def _kick_extended_args(prepared_kick: PreparedKick) -> tuple:
        plan = prepared_kick.recovery_plan or KickRecoveryPlan()
        return (
            (
                to_checksum_address(prepared_kick.candidate.source_address),
                to_checksum_address(prepared_kick.candidate.auction_address),
                to_checksum_address(prepared_kick.candidate.token_address),
                prepared_kick.sell_amount,
                to_checksum_address(prepared_kick.candidate.want_address),
                prepared_kick.starting_price_unscaled,
                prepared_kick.minimum_price_scaled_1e18,
                prepared_kick.step_decay_rate_bps,
                (
                    to_checksum_address(prepared_kick.settle_token)
                    if prepared_kick.settle_token
                    else "0x0000000000000000000000000000000000000000"
                ),
                [to_checksum_address(address) for address in plan.settle_after_start],
                [to_checksum_address(address) for address in plan.settle_after_min],
                [to_checksum_address(address) for address in plan.settle_after_decay],
            ),
        )
