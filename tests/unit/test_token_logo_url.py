import pytest

from tidal.constants import CVX_ADDRESS, CVX_PRICE_ALIAS_ADDRESS, CVX_WRAPPER_ALIAS_ADDRESS
from tidal.errors import AddressNormalizationError
from tidal.pricing.token_logo import TOKEN_LOGO_ORIGIN, token_logo_url


def test_token_logo_url_is_stable_and_lowercase() -> None:
    address = "0xA0b86991C6218b36c1D19d4A2e9Eb0cE3606eB48"

    assert token_logo_url(chain_id=1, address=address) == (
        f"{TOKEN_LOGO_ORIGIN}/token-logos/1/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    )


@pytest.mark.parametrize("alias", [CVX_PRICE_ALIAS_ADDRESS, CVX_WRAPPER_ALIAS_ADDRESS])
def test_token_logo_url_preserves_explicit_cvx_alias_behavior(alias: str) -> None:
    assert token_logo_url(chain_id=1, address=alias) == (
        f"{TOKEN_LOGO_ORIGIN}/token-logos/1/{CVX_ADDRESS}"
    )


def test_token_logo_url_rejects_non_positive_chain_id() -> None:
    with pytest.raises(ValueError, match="chain_id must be positive"):
        token_logo_url(chain_id=0, address=CVX_ADDRESS)


def test_token_logo_url_rejects_invalid_address() -> None:
    with pytest.raises(AddressNormalizationError):
        token_logo_url(chain_id=1, address="not-an-address")
