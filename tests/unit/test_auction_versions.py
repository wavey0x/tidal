import pytest

from tidal.auction_versions import (
    APPROVED_AUCTION_SPECS,
    AUCTION_V104_FACTORY_ADDRESS,
    AUCTION_V105_FACTORY_ADDRESS,
    StartingPriceEncoding,
    approved_auction_spec_for_factory,
    approved_auction_spec_for_version,
)
from tidal.chain.contracts.abis import AUCTION_VERSION_ABI, SUPPORTED_AUCTION_ABI, SUPPORTED_AUCTION_FACTORY_ABI


def test_approved_factory_and_version_registry_is_exact() -> None:
    assert [(spec.factory_address, spec.version) for spec in APPROVED_AUCTION_SPECS] == [
        ("0xba7fcb508c7195ee5ae823f37ee2c11d7ed52f8e", "1.0.4"),
        ("0x55b3830b4d85e6868c73f00a2e857e9adbf89568", "1.0.5"),
    ]
    assert (
        approved_auction_spec_for_version("1.0.4").starting_price_encoding
        == StartingPriceEncoding.WHOLE_WANT
    )
    assert (
        approved_auction_spec_for_factory(AUCTION_V105_FACTORY_ADDRESS).starting_price_encoding
        == StartingPriceEncoding.WAD_WANT
    )

    with pytest.raises(ValueError, match="unsupported auction version"):
        approved_auction_spec_for_version("1.0.3cc")
    with pytest.raises(ValueError, match="unsupported auction factory"):
        approved_auction_spec_for_factory("0x1111111111111111111111111111111111111111")


def test_supported_auction_abis_cover_every_tidal_call() -> None:
    auction_functions = {
        item["name"]: (
            tuple(value["type"] for value in item.get("inputs", ())),
            tuple(value["type"] for value in item.get("outputs", ())),
        )
        for item in SUPPORTED_AUCTION_ABI
        if item.get("type") == "function"
    }
    assert {name: auction_functions[name] for name in (
        "version",
        "governance",
        "want",
        "receiver",
        "isAnActiveAuction",
        "auctionLength",
        "stepDuration",
        "minimumPrice",
        "auctions",
        "setStartingPrice",
        "setMinimumPrice",
        "setStepDecayRate",
        "kick",
    )} == {
        "version": ((), ("string",)),
        "governance": ((), ("address",)),
        "want": ((), ("address",)),
        "receiver": ((), ("address",)),
        "isAnActiveAuction": ((), ("bool",)),
        "auctionLength": ((), ("uint256",)),
        "stepDuration": ((), ("uint256",)),
        "minimumPrice": ((), ("uint256",)),
        "auctions": (("address",), ("uint64", "uint64", "uint128")),
        "setStartingPrice": (("uint256",), ()),
        "setMinimumPrice": (("uint256",), ()),
        "setStepDecayRate": (("uint256",), ()),
        "kick": (("address",), ("uint256",)),
    }

    assert [item["name"] for item in AUCTION_VERSION_ABI] == ["version"]

    factory_functions = {
        item["name"]: (
            tuple(value["type"] for value in item.get("inputs", ())),
            tuple(value["type"] for value in item.get("outputs", ())),
        )
        for item in SUPPORTED_AUCTION_FACTORY_ABI
        if item.get("type") == "function"
    }
    assert factory_functions == {
        "getAllAuctions": ((), ("address[]",)),
        "createNewAuction": (
            ("address", "address", "address", "uint256", "bytes32"),
            ("address",),
        ),
    }
