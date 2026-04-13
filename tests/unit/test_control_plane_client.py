import httpx

from tidal.control_plane.client import ControlPlaneClient, ControlPlaneError


def test_request_reports_non_json_error_body() -> None:
    client = ControlPlaneClient(base_url="https://api.example.com", token="secret")

    def fake_request(method: str, path: str, *, params=None, json=None) -> httpx.Response:  # noqa: ANN001
        del method, path, params, json
        request = httpx.Request("POST", "https://api.example.com/api/v1/tidal/actions/demo/broadcast")
        return httpx.Response(500, text="Internal Server Error", request=request)

    client._client.request = fake_request  # type: ignore[method-assign]  # noqa: SLF001

    try:
        client.report_broadcast(
            "demo",
            {
                "sender": "0x6000000000000000000000000000000000000006",
                "txHash": "0xabc",
                "broadcastAt": "2026-03-28T00:01:00+00:00",
                "txIndex": 0,
            },
        )
    except ControlPlaneError as exc:
        assert str(exc) == "API returned 500: Internal Server Error"
        assert exc.status_code == 500
    else:
        raise AssertionError("expected ControlPlaneError")


def test_request_wraps_transport_errors() -> None:
    client = ControlPlaneClient(base_url="https://api.example.com", token="secret")

    def fake_request(method: str, path: str, *, params=None, json=None) -> httpx.Response:  # noqa: ANN001
        del method, path, params, json
        raise httpx.ConnectError("connection refused")

    client._client.request = fake_request  # type: ignore[method-assign]  # noqa: SLF001

    try:
        client.get_action("demo")
    except ControlPlaneError as exc:
        assert str(exc) == "Could not reach Tidal API at https://api.example.com: connection refused"
        assert exc.status_code is None
    else:
        raise AssertionError("expected ControlPlaneError")


def test_request_maps_invalid_bearer_token_error() -> None:
    client = ControlPlaneClient(base_url="https://api.example.com", token="secret")

    def fake_request(method: str, path: str, *, params=None, json=None) -> httpx.Response:  # noqa: ANN001
        del method, params, json
        request = httpx.Request("GET", f"https://api.example.com{path}")
        return httpx.Response(
            401,
            json={"status": "error", "warnings": [], "data": None, "detail": "Invalid bearer token"},
            request=request,
        )

    client._client.request = fake_request  # type: ignore[method-assign]  # noqa: SLF001

    try:
        client.verify_authenticated_access()
    except ControlPlaneError as exc:
        assert str(exc) == "TIDAL_API_KEY is invalid for Tidal API at https://api.example.com"
        assert exc.status_code == 401
    else:
        raise AssertionError("expected ControlPlaneError")


def test_request_maps_invalid_bearer_token_http_exception_body() -> None:
    client = ControlPlaneClient(base_url="https://api.example.com", token="secret")

    def fake_request(method: str, path: str, *, params=None, json=None) -> httpx.Response:  # noqa: ANN001
        del method, params, json
        request = httpx.Request("GET", f"https://api.example.com{path}")
        return httpx.Response(401, json={"detail": "Invalid bearer token"}, request=request)

    client._client.request = fake_request  # type: ignore[method-assign]  # noqa: SLF001

    try:
        client.verify_authenticated_access()
    except ControlPlaneError as exc:
        assert str(exc) == "TIDAL_API_KEY is invalid for Tidal API at https://api.example.com"
        assert exc.status_code == 401
    else:
        raise AssertionError("expected ControlPlaneError")


def test_request_maps_missing_server_keys_error() -> None:
    client = ControlPlaneClient(base_url="https://api.example.com", token="secret")

    def fake_request(method: str, path: str, *, params=None, json=None) -> httpx.Response:  # noqa: ANN001
        del method, params, json
        request = httpx.Request("GET", f"https://api.example.com{path}")
        return httpx.Response(
            503,
            json={"status": "error", "warnings": [], "data": None, "detail": "No API keys configured"},
            request=request,
        )

    client._client.request = fake_request  # type: ignore[method-assign]  # noqa: SLF001

    try:
        client.verify_authenticated_access()
    except ControlPlaneError as exc:
        assert str(exc) == "Tidal API at https://api.example.com has no API keys configured"
        assert exc.status_code == 503
    else:
        raise AssertionError("expected ControlPlaneError")


def test_request_surfaces_plain_json_detail_for_prepare_errors() -> None:
    client = ControlPlaneClient(base_url="https://api.example.com", token="secret")

    def fake_request(method: str, path: str, *, params=None, json=None) -> httpx.Response:  # noqa: ANN001
        del method, params, json
        request = httpx.Request("POST", f"https://api.example.com{path}")
        return httpx.Response(
            409,
            json={"detail": "auction is not in the configured factory"},
            request=request,
        )

    client._client.request = fake_request  # type: ignore[method-assign]  # noqa: SLF001

    try:
        client.prepare_enable_tokens(
            "0x3000000000000000000000000000000000000003",
            {"sender": "0x6000000000000000000000000000000000000006", "extraTokens": []},
        )
    except ControlPlaneError as exc:
        assert str(exc) == "API returned 409: auction is not in the configured factory"
        assert exc.status_code == 409
    else:
        raise AssertionError("expected ControlPlaneError")


def test_prepare_enable_tokens_returns_error_payload_without_raising() -> None:
    client = ControlPlaneClient(base_url="https://api.example.com", token="secret")
    payload = {
        "status": "error",
        "warnings": ["failed to load auction metadata for 0xabc: execution reverted"],
        "data": {"preview": {}, "transactions": []},
    }

    def fake_request(method: str, path: str, *, params=None, json=None) -> httpx.Response:  # noqa: ANN001
        del method, params, json
        request = httpx.Request("POST", f"https://api.example.com{path}")
        return httpx.Response(200, json=payload, request=request)

    client._client.request = fake_request  # type: ignore[method-assign]  # noqa: SLF001

    response = client.prepare_enable_tokens(
        "0x3000000000000000000000000000000000000003",
        {"sender": "0x6000000000000000000000000000000000000006", "extraTokens": []},
    )

    assert response == payload
