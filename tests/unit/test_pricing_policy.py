from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from tidal.resources import read_template_text
from tidal.auction_price_units import latent_terminal_full_lot_ask_raw
from tidal.auction_versions import StartingPriceEncoding
from tidal.transaction_service.kick_policy import build_kick_config, load_kick_config


@pytest.mark.parametrize("packaged", [False, True], ids=["server", "packaged"])
def test_msethweth_small_lot_profile_is_scoped_and_reaches_quote(packaged):
    text = (
        read_template_text("server.yaml")
        if packaged
        else (Path(__file__).resolve().parents[2] / "config/server.yaml").read_text()
    )
    policy = build_kick_config(yaml.safe_load(text)["kick"]).pricing_policy
    auction = "0x737c6ff4b4e13935b2b4e785047ab097993c9d0e"
    crv = "0xd533a949740bb3306d119cc777fa900ba034cd52"
    other = "0x0000000000000000000000000000000000000001"
    profile = policy.resolve(auction, crv)
    default = policy.profiles[policy.default_profile_name]

    assert policy.resolve(auction, other) == default
    assert policy.resolve(other, crv) == default
    assert profile.start_price_buffer_bps == default.start_price_buffer_bps
    assert profile.min_price_buffer_bps == default.min_price_buffer_bps == 500
    assert profile.outlier_floor_enabled is default.outlier_floor_enabled is True

    # This real v1.0.4 lot cannot reach its quote in 24 hours at 15 bps.
    pricing = dict(
        encoding=StartingPriceEncoding.WHOLE_WANT,
        starting_price_raw=1,
        sell_amount_raw=673977045652087330052,
        sell_decimals=18,
        want_decimals=18,
        step_duration_seconds=60,
        auction_length_seconds=86400,
    )
    quote_raw = 93664022270628838
    assert latent_terminal_full_lot_ask_raw(**pricing, step_decay_rate_bps=15) > quote_raw
    assert latent_terminal_full_lot_ask_raw(
        **pricing, step_decay_rate_bps=profile.step_decay_rate_bps
    ) < quote_raw


def test_load_kick_config_reads_default_and_token_overrides(tmp_path):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        """
default_profile: volatile
kick_limit_buffer_bps: 1000

no_fill:
  retry_delays_minutes: [720, 1440]

profiles:
  volatile:
    start_price_buffer_bps: 1000
    min_price_buffer_bps: 500
    step_decay_rate_bps: 50

default_usd_kick_limit: 3000

usd_kick_limit:
  "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": 5000
  "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": 25000

cooldown_minutes: 60
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_kick_config(kick_path)

    rule_a = config.token_sizing_policy.resolve("0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    rule_b = config.token_sizing_policy.resolve("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    rule_missing = config.token_sizing_policy.resolve("0xcccccccccccccccccccccccccccccccccccccccc")

    assert rule_a == Decimal("5000")
    assert rule_b == Decimal("25000")
    assert rule_missing == Decimal("3000")
    assert config.token_sizing_policy.kick_limit_buffer_bps == 1000
    assert config.pricing_policy.default_profile_name == "volatile"
    assert config.cooldown_policy.default_minutes == 60


def test_load_kick_config_defaults_to_empty_overrides_when_absent(tmp_path):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        """
default_profile: volatile
kick_limit_buffer_bps: 1000

no_fill:
  retry_delays_minutes: [720, 1440]

profiles:
  volatile:
    start_price_buffer_bps: 1000
    min_price_buffer_bps: 500
    step_decay_rate_bps: 50

cooldown_minutes: 60
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_kick_config(kick_path)

    assert config.token_sizing_policy.default_limit is None
    assert config.token_sizing_policy.token_overrides == {}
    assert (
        config.token_sizing_policy.resolve("0xcccccccccccccccccccccccccccccccccccccccc")
        is None
    )
    assert config.ignore_policy.ignored_sources == frozenset()
    assert config.cooldown_policy.auction_token_overrides_minutes == {}


def test_load_kick_config_parses_profile_overrides(tmp_path):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        """
default_profile: volatile
kick_limit_buffer_bps: 1000

no_fill:
  retry_delays_minutes: [720, 1440]

profiles:
  volatile:
    start_price_buffer_bps: 1000
    min_price_buffer_bps: 500
    step_decay_rate_bps: 50

  stable:
    start_price_buffer_bps: 100
    min_price_buffer_bps: 50
    step_decay_rate_bps: 2
    outlier_floor_enabled: true

profile_overrides:
  - auction: "0x1111111111111111111111111111111111111111"
    token: "0x2222222222222222222222222222222222222222"
    profile: stable
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_kick_config(kick_path)

    default_profile = config.pricing_policy.resolve(
        "0x3333333333333333333333333333333333333333",
        "0x4444444444444444444444444444444444444444",
    )
    override_profile = config.pricing_policy.resolve(
        "0x1111111111111111111111111111111111111111",
        "0x2222222222222222222222222222222222222222",
    )

    assert default_profile.name == "volatile"
    assert default_profile.outlier_floor_enabled is False
    assert override_profile.name == "stable"
    assert override_profile.outlier_floor_enabled is True


def test_load_kick_config_parses_ignore_and_cooldown_rules(tmp_path):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        """
default_profile: volatile
kick_limit_buffer_bps: 1000

no_fill:
  retry_delays_minutes: [720, 1440]

profiles:
  volatile:
    start_price_buffer_bps: 1000
    min_price_buffer_bps: 500
    step_decay_rate_bps: 50

ignore:
  - source: "0x1111111111111111111111111111111111111111"
  - auction: "0x2222222222222222222222222222222222222222"
  - auction: "0x3333333333333333333333333333333333333333"
    token: "0x4444444444444444444444444444444444444444"

cooldown_minutes: 60

cooldown:
  - auction: "0x5555555555555555555555555555555555555555"
    token: "0x6666666666666666666666666666666666666666"
    minutes: 180
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_kick_config(kick_path)

    assert "0x1111111111111111111111111111111111111111" in config.ignore_policy.ignored_sources
    assert "0x2222222222222222222222222222222222222222" in config.ignore_policy.ignored_auctions
    assert (
        "0x3333333333333333333333333333333333333333",
        "0x4444444444444444444444444444444444444444",
    ) in config.ignore_policy.ignored_auction_tokens
    assert config.cooldown_policy.resolve_minutes(
        auction_address="0x5555555555555555555555555555555555555555",
        token_address="0x6666666666666666666666666666666666666666",
    ) == 180


def test_load_kick_config_rejects_legacy_auctions_key(tmp_path):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        """
default_profile: volatile
kick_limit_buffer_bps: 1000

profiles:
  volatile:
    start_price_buffer_bps: 1000
    min_price_buffer_bps: 500
    step_decay_rate_bps: 50

auctions:
  "0x1111111111111111111111111111111111111111":
    "0x2222222222222222222222222222222222222222": volatile
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="profile_overrides"):
        load_kick_config(kick_path)


@pytest.mark.parametrize(
    ("no_fill_yaml", "message"),
    [
        ("", "must define no_fill"),
        ("no_fill:\n  retry_delays_minutes: []\n", "non-empty list"),
        ("no_fill:\n  retry_delays_minutes: [720, 720]\n", "strictly increasing"),
        ("no_fill:\n  retry_delays_minutes: [0, 1440]\n", "positive integer"),
    ],
)
def test_load_kick_config_requires_strict_no_fill_schedule(tmp_path, no_fill_yaml, message):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        (
            "default_profile: volatile\n"
            "kick_limit_buffer_bps: 1000\n"
            f"{no_fill_yaml}"
            "profiles:\n"
            "  volatile:\n"
            "    start_price_buffer_bps: 1000\n"
            "    min_price_buffer_bps: 500\n"
            "    step_decay_rate_bps: 50\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_kick_config(kick_path)


def test_load_kick_config_rejects_duplicate_profile_overrides(tmp_path):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        """
default_profile: volatile
kick_limit_buffer_bps: 1000

profiles:
  volatile:
    start_price_buffer_bps: 1000
    min_price_buffer_bps: 500
    step_decay_rate_bps: 50

profile_overrides:
  - auction: "0x1111111111111111111111111111111111111111"
    token: "0x2222222222222222222222222222222222222222"
    profile: volatile
  - auction: "0x1111111111111111111111111111111111111111"
    token: "0x2222222222222222222222222222222222222222"
    profile: volatile
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate profile override"):
        load_kick_config(kick_path)


def test_load_kick_config_accepts_packaged_kick_template(tmp_path):
    server_raw = yaml.safe_load(read_template_text("server.yaml"))
    config = build_kick_config(server_raw["kick"])

    stable_profile = config.pricing_policy.resolve(
        "0xA00E6b35C23442fa9D5149Cba5dd94623fFE6693",
        "0x2A8e1E676Ec238d8A992307B495b45B3fEAa5e86",
    )
    eva_usdt_profile = config.pricing_policy.resolve(
        "0xA00E6b35C23442fa9D5149Cba5dd94623fFE6693",
        "0x501eBf66d76A96D4FB26ccead42957653e16B8B8",
    )
    default_profile = config.pricing_policy.profiles["volatile"]
    semi_volatile_profile = config.pricing_policy.profiles["semi-volatile"]

    assert config.pricing_policy.default_profile_name == "volatile"
    assert default_profile.outlier_floor_enabled is True
    assert semi_volatile_profile.outlier_floor_enabled is True
    assert stable_profile.name == "stable"
    assert stable_profile.outlier_floor_enabled is True
    assert eva_usdt_profile.name == "stable"
    assert config.token_sizing_policy.default_limit == Decimal("3000")
    assert config.token_sizing_policy.kick_limit_buffer_bps == 1000
    assert (
        config.token_sizing_policy.token_overrides[
            "0x419905009e4656fdc02418c7df35b1e61ed5f726"
        ]
        == Decimal("3000")
    )
    assert (
        config.token_sizing_policy.resolve("0x0000000000000000000000000000000000000001")
        == Decimal("3000")
    )
    assert (
        config.ignore_policy.match(
            source_address="0xC69aA6Cd632A88424ceAf3688F295B856eB82287",
            auction_address="0x0000000000000000000000000000000000000001",
            token_address="0x0000000000000000000000000000000000000002",
        )
        == "source"
    )
    assert (
        config.ignore_policy.match(
            source_address="0xe1E426113DC75480bB6838520dEFA7dF92627b9A",
            auction_address="0x0000000000000000000000000000000000000001",
            token_address="0x0000000000000000000000000000000000000002",
        )
        == "source"
    )
    assert (
        config.ignore_policy.match(
            source_address="0x0000000000000000000000000000000000000001",
            auction_address="0xcCE8031B58b42e900Aa2c1F23CD00B0939D1d675",
            token_address="0x0000000000000000000000000000000000000002",
        )
        == "auction"
    )
    assert (
        config.cooldown_policy.resolve_minutes(
            auction_address="0xA00E6b35C23442fa9D5149Cba5dd94623fFE6693",
            token_address="0x04ACaF8D2865c0714F79da09645C13FD2888977f",
        )
        == 10
    )
    assert (
        config.cooldown_policy.resolve_minutes(
            auction_address="0xA00E6b35C23442fa9D5149Cba5dd94623fFE6693",
            token_address="0x419905009e4656fdC02418C7Df35B1E61Ed5F726",
        )
        == 1440
    )


@pytest.mark.parametrize("buffer_bps", [0, 250, 1000, 2500])
def test_kick_limit_buffer_accepts_non_negative_integers(buffer_bps):
    raw = yaml.safe_load(read_template_text("server.yaml"))["kick"]
    raw["kick_limit_buffer_bps"] = buffer_bps
    assert build_kick_config(raw).token_sizing_policy.kick_limit_buffer_bps == buffer_bps


@pytest.mark.parametrize("buffer_bps", [None, -1, True, False, 1.5, 1000.0, "1000", "bad"])
def test_kick_limit_buffer_rejects_invalid_values(buffer_bps):
    raw = yaml.safe_load(read_template_text("server.yaml"))["kick"]
    raw["kick_limit_buffer_bps"] = buffer_bps
    with pytest.raises(ValueError, match="kick_limit_buffer_bps must be a non-negative integer"):
        build_kick_config(raw)


def test_kick_limit_buffer_is_required():
    raw = yaml.safe_load(read_template_text("server.yaml"))["kick"]
    del raw["kick_limit_buffer_bps"]
    with pytest.raises(ValueError, match="kick_limit_buffer_bps"):
        build_kick_config(raw)
