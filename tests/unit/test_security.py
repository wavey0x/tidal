from tidal.security import redact_sensitive_data, redact_sensitive_text


def test_redact_sensitive_text_masks_credentials_in_urls_and_tokens() -> None:
    raw = (
        "RPC failed for https://alice:supersecret@rpc.example/v1/mainnet?api_key=abc123&network=mainnet "
        "Authorization: Bearer secret-token TOKEN_PRICE_AGG_KEY=xyz789"
    )

    redacted = redact_sensitive_text(raw)

    assert redacted is not None
    assert "supersecret" not in redacted
    assert "abc123" not in redacted
    assert "secret-token" not in redacted
    assert "xyz789" not in redacted
    assert "https://REDACTED:REDACTED@rpc.example/v1/mainnet?api_key=REDACTED&network=mainnet" in redacted
    assert "Authorization: Bearer REDACTED" in redacted
    assert "TOKEN_PRICE_AGG_KEY=REDACTED" in redacted


def test_redact_sensitive_data_walks_nested_structures() -> None:
    payload = {
        "detail": "Bearer secret-token",
        "nested": [
            "https://user:pass@example.com/path?access_token=abc",
            {"message": "ETH_PASSWORD=hunter2"},
        ],
    }

    redacted = redact_sensitive_data(payload)

    assert redacted["detail"] == "Bearer REDACTED"
    assert redacted["nested"][0] == "https://REDACTED:REDACTED@example.com/path?access_token=REDACTED"
    assert redacted["nested"][1]["message"] == "ETH_PASSWORD=REDACTED"
