from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from tidal.pricing.token_logo import TokenLogoValidator


@pytest.mark.asyncio
async def test_validate_none_returns_not_found() -> None:
    validator = TokenLogoValidator(timeout_seconds=5, retry_attempts=1)
    result = await validator.validate(None)
    assert result.status == "NOT_FOUND"
    assert result.logo_url is None
    assert result.error_message is None


@pytest.mark.asyncio
async def test_validate_empty_string_returns_not_found() -> None:
    validator = TokenLogoValidator(timeout_seconds=5, retry_attempts=1)
    result = await validator.validate("  ")
    assert result.status == "NOT_FOUND"
    assert result.logo_url is None


@pytest.mark.asyncio
async def test_validate_invalid_scheme_returns_invalid() -> None:
    validator = TokenLogoValidator(timeout_seconds=5, retry_attempts=1)
    result = await validator.validate("ftp://example.com/logo.png")
    assert result.status == "INVALID"
    assert result.logo_url is None
    assert result.error_message == "invalid logo url"


@pytest.mark.asyncio
async def test_validate_no_host_returns_invalid() -> None:
    validator = TokenLogoValidator(timeout_seconds=5, retry_attempts=1)
    result = await validator.validate("https://")
    assert result.status == "INVALID"
    assert result.logo_url is None


def _mock_stream_response(status_code: int, headers: dict[str, str] | None = None):
    """Create a mock that works with `async with client.stream(...) as response`."""
    response = MagicMock()
    response.status_code = status_code
    response.headers = httpx.Headers(headers or {})

    stream_ctx = AsyncMock()
    stream_ctx.__aenter__ = AsyncMock(return_value=response)
    stream_ctx.__aexit__ = AsyncMock(return_value=False)
    return stream_ctx


@pytest.mark.asyncio
async def test_validate_success() -> None:
    url = "https://cdn.example.com/token.png"
    validator = TokenLogoValidator(timeout_seconds=5, retry_attempts=1)

    mock_ctx = _mock_stream_response(200, {"content-type": "image/png"})
    with patch("httpx.AsyncClient.stream", return_value=mock_ctx):
        result = await validator.validate(url)

    assert result.status == "SUCCESS"
    assert result.logo_url == url
    assert result.error_message is None


@pytest.mark.asyncio
async def test_validate_404_returns_not_found() -> None:
    url = "https://cdn.example.com/missing.png"
    validator = TokenLogoValidator(timeout_seconds=5, retry_attempts=1)

    mock_ctx = _mock_stream_response(404)
    with patch("httpx.AsyncClient.stream", return_value=mock_ctx):
        result = await validator.validate(url)

    assert result.status == "NOT_FOUND"
    assert result.logo_url is None
    assert "404" in result.error_message


@pytest.mark.asyncio
async def test_validate_500_returns_failed() -> None:
    url = "https://cdn.example.com/error.png"
    validator = TokenLogoValidator(timeout_seconds=5, retry_attempts=1)

    mock_ctx = _mock_stream_response(500)
    with patch("httpx.AsyncClient.stream", return_value=mock_ctx):
        result = await validator.validate(url)

    assert result.status == "FAILED"
    assert result.logo_url is None
    assert "500" in result.error_message


@pytest.mark.asyncio
async def test_validate_non_image_content_type_returns_invalid() -> None:
    url = "https://cdn.example.com/page.html"
    validator = TokenLogoValidator(timeout_seconds=5, retry_attempts=1)

    mock_ctx = _mock_stream_response(200, {"content-type": "text/html"})
    with patch("httpx.AsyncClient.stream", return_value=mock_ctx):
        result = await validator.validate(url)

    assert result.status == "INVALID"
    assert result.logo_url is None
    assert "text/html" in result.error_message


@pytest.mark.asyncio
async def test_validate_missing_content_type_returns_invalid() -> None:
    url = "https://cdn.example.com/noheader"
    validator = TokenLogoValidator(timeout_seconds=5, retry_attempts=1)

    mock_ctx = _mock_stream_response(200, {})
    with patch("httpx.AsyncClient.stream", return_value=mock_ctx):
        result = await validator.validate(url)

    assert result.status == "INVALID"
    assert result.logo_url is None


@pytest.mark.asyncio
async def test_validate_svg_content_type_succeeds() -> None:
    url = "https://cdn.example.com/token.svg"
    validator = TokenLogoValidator(timeout_seconds=5, retry_attempts=1)

    mock_ctx = _mock_stream_response(200, {"content-type": "image/svg+xml"})
    with patch("httpx.AsyncClient.stream", return_value=mock_ctx):
        result = await validator.validate(url)

    assert result.status == "SUCCESS"
    assert result.logo_url == url


@pytest.mark.asyncio
async def test_validate_connection_error_returns_failed() -> None:
    url = "https://cdn.example.com/logo.png"
    validator = TokenLogoValidator(timeout_seconds=5, retry_attempts=1)

    with patch("httpx.AsyncClient.stream", side_effect=httpx.ConnectError("connection refused")):
        result = await validator.validate(url)

    assert result.status == "FAILED"
    assert result.logo_url is None
    assert result.error_message is not None
