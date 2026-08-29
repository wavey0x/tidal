"""Approved auction factories and their starting-price semantics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from tidal.normalizers import normalize_address


class StartingPriceEncoding(str, Enum):
    WHOLE_WANT = "whole_want"
    WAD_WANT = "wad_want"


@dataclass(frozen=True, slots=True)
class ApprovedAuctionSpec:
    factory_address: str
    version: str
    starting_price_encoding: StartingPriceEncoding
    mapping_priority: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "factory_address", normalize_address(self.factory_address))


AUCTION_V104_FACTORY_ADDRESS = "0xba7fcb508c7195ee5ae823f37ee2c11d7ed52f8e"
AUCTION_V105_FACTORY_ADDRESS = "0x55b3830b4d85e6868c73f00a2e857e9adbf89568"

APPROVED_AUCTION_SPECS = (
    ApprovedAuctionSpec(
        factory_address=AUCTION_V104_FACTORY_ADDRESS,
        version="1.0.4",
        starting_price_encoding=StartingPriceEncoding.WHOLE_WANT,
        mapping_priority=1,
    ),
    ApprovedAuctionSpec(
        factory_address=AUCTION_V105_FACTORY_ADDRESS,
        version="1.0.5",
        starting_price_encoding=StartingPriceEncoding.WAD_WANT,
        mapping_priority=2,
    ),
)

_SPEC_BY_FACTORY = {spec.factory_address: spec for spec in APPROVED_AUCTION_SPECS}
_SPEC_BY_VERSION = {spec.version: spec for spec in APPROVED_AUCTION_SPECS}


def approved_auction_spec_for_factory(factory_address: str) -> ApprovedAuctionSpec:
    normalized = normalize_address(factory_address)
    try:
        return _SPEC_BY_FACTORY[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported auction factory: {normalized}") from exc


def approved_auction_spec_for_version(version: str | None) -> ApprovedAuctionSpec:
    normalized = str(version or "").strip()
    try:
        return _SPEC_BY_VERSION[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported auction version: {normalized or '<missing>'}") from exc
