"""Helpers for scanning configured fee burners."""

from __future__ import annotations

from factory_dashboard.chain.contracts.fee_burner import FeeBurnerReader
from factory_dashboard.config import MonitoredFeeBurner
from factory_dashboard.normalizers import normalize_address
from factory_dashboard.types import ScanItemError


class FeeBurnerTokenResolver:
    """Resolves approved sell tokens for configured fee burners."""

    def __init__(self, fee_burner_reader: FeeBurnerReader, *, spender_address: str):
        self.fee_burner_reader = fee_burner_reader
        self.spender_address = normalize_address(spender_address)

    async def resolve_many(
        self,
        fee_burners: list[MonitoredFeeBurner],
    ) -> tuple[dict[str, set[str]], list[ScanItemError]]:
        tokens_by_burner: dict[str, set[str]] = {}
        errors: list[ScanItemError] = []

        for fee_burner in fee_burners:
            address = normalize_address(fee_burner.address)
            try:
                if not await self.fee_burner_reader.is_token_spender(address, self.spender_address):
                    errors.append(
                        ScanItemError(
                            stage="TOKEN_RESOLUTION",
                            error_code="fee_burner_spender_not_allowed",
                            error_message=f"{self.spender_address} is not an allowed token spender",
                            source_type="fee_burner",
                            source_address=address,
                        )
                    )
                    tokens_by_burner[address] = set()
                    continue

                approvals = await self.fee_burner_reader.get_approvals(address, self.spender_address)
                tokens_by_burner[address] = set(approvals)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    ScanItemError(
                        stage="TOKEN_RESOLUTION",
                        error_code="fee_burner_approvals_read_failed",
                        error_message=str(exc),
                        source_type="fee_burner",
                        source_address=address,
                    )
                )
                tokens_by_burner[address] = set()

        return tokens_by_burner, errors
