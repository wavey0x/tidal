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
    else:
        raise AssertionError("expected ControlPlaneError")
