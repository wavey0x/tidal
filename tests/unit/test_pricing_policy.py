from decimal import Decimal

import pytest
import yaml

from tidal.resources import read_template_text
from tidal.transaction_service.kick_policy import build_kick_config, load_kick_config


def test_load_kick_config_reads_token_overrides(tmp_path):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        """
default_profile: volatile

profiles:
  volatile:
    start_price_buffer_bps: 1000
    min_price_buffer_bps: 500
    step_decay_rate_bps: 50

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
    assert rule_missing is None
    assert config.pricing_policy.default_profile_name == "volatile"
    assert config.cooldown_policy.default_minutes == 60


def test_load_kick_config_defaults_to_empty_overrides_when_absent(tmp_path):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        """
default_profile: volatile

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

    assert config.token_sizing_policy.token_overrides == {}
    assert config.ignore_policy.ignored_sources == frozenset()
    assert config.cooldown_policy.auction_token_overrides_minutes == {}


def test_load_kick_config_parses_profile_overrides(tmp_path):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        """
default_profile: volatile

profiles:
  volatile:
    start_price_buffer_bps: 1000
    min_price_buffer_bps: 500
    step_decay_rate_bps: 50

  stable:
    start_price_buffer_bps: 100
    min_price_buffer_bps: 50
    step_decay_rate_bps: 2

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
    assert override_profile.name == "stable"


def test_load_kick_config_parses_ignore_and_cooldown_rules(tmp_path):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        """
default_profile: volatile

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


def test_load_kick_config_rejects_duplicate_profile_overrides(tmp_path):
    kick_path = tmp_path / "kick.yaml"
    kick_path.write_text(
        """
default_profile: volatile

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

    assert config.pricing_policy.default_profile_name == "volatile"
    assert stable_profile.name == "stable"
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
