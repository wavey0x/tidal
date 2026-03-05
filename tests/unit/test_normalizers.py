from tidal.normalizers import normalize_address, to_decimal_string


def test_normalize_address_lowercases() -> None:
    mixed = "0x4e3FBD56CD56c3e72c1403e103b45Db9da5B9D2B"
    assert normalize_address(mixed) == "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b"


def test_to_decimal_string_integer() -> None:
    assert to_decimal_string(1000000000000000000, 18) == "1"


def test_to_decimal_string_fraction() -> None:
    assert to_decimal_string(123456, 6) == "0.123456"


def test_to_decimal_zero() -> None:
    assert to_decimal_string(0, 18) == "0"
