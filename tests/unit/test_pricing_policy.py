from decimal import Decimal

from tidal.transaction_service.pricing_policy import load_token_sizing_policy


def test_load_token_sizing_policy_reads_token_overrides(tmp_path):
    policy_path = tmp_path / "auction_pricing_policy.yaml"
    policy_path.write_text(
        """
usd_kick_limit:
  "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": 5000
  "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": 25000
""".strip()
        + "\n",
        encoding="utf-8",
    )

    policy = load_token_sizing_policy(policy_path)

    rule_a = policy.resolve("0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    rule_b = policy.resolve("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    rule_missing = policy.resolve("0xcccccccccccccccccccccccccccccccccccccccc")

    assert rule_a == Decimal("5000")
    assert rule_b == Decimal("25000")
    assert rule_missing is None


def test_load_token_sizing_policy_defaults_to_empty_when_absent(tmp_path):
    policy_path = tmp_path / "auction_pricing_policy.yaml"
    policy_path.write_text("profiles: {}\n", encoding="utf-8")

    policy = load_token_sizing_policy(policy_path)

    assert policy.token_overrides == {}
