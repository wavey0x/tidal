"""Shared HTTP client for the Tidal control-plane API."""

from __future__ import annotations

from typing import Any

import httpx

_EXPECTED_RESPONSE_KEYS = frozenset({"status", "warnings", "data"})


def _looks_like_tidal_response(payload: object) -> bool:
    return isinstance(payload, dict) and _EXPECTED_RESPONSE_KEYS.issubset(payload)


def _extract_error_detail(payload: object) -> object | None:
    if not isinstance(payload, dict):
        return None
    for key in ("detail", "message", "error"):
        if key in payload:
            return payload.get(key)
    return None


class ControlPlaneError(RuntimeError):
    """Raised when the control-plane API returns an error."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ControlPlaneClient:
    """Small wrapper around the monorepo FastAPI control plane."""

    def __init__(self, *, base_url: str, token: str, timeout_seconds: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ControlPlaneClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        del exc_type, exc, tb
        self.close()

    def _target_url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _transport_error_message(self, exc: httpx.HTTPError) -> str:
        if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
            return f"Invalid TIDAL_API_BASE_URL '{self.base_url}': {exc}"
        return f"Could not reach Tidal API at {self.base_url}: {exc}"

    def _unexpected_response_error(self, *, path: str, status_code: int | None = None) -> ControlPlaneError:
        return ControlPlaneError(
            f"Unexpected response from Tidal API at {self._target_url(path)}; check TIDAL_API_BASE_URL",
            status_code=status_code,
        )

    def _api_error_message(self, *, status_code: int, detail: object | None) -> str:
        detail_text = str(detail).strip() if detail is not None else ""
        if status_code == 401 and detail_text == "Bearer token required":
            return "TIDAL_API_KEY is required for this command"
        if status_code == 401 and detail_text == "Invalid bearer token":
            return f"TIDAL_API_KEY is invalid for Tidal API at {self.base_url}"
        if status_code == 503 and detail_text == "No API keys configured":
            return f"Tidal API at {self.base_url} has no API keys configured"
        if detail_text:
            return f"API returned {status_code}: {detail_text}"
        return f"API returned HTTP {status_code}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise ControlPlaneError(self._transport_error_message(exc)) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise self._unexpected_response_error(path=path, status_code=response.status_code) from exc

        if not response.is_success:
            message = _extract_error_detail(payload)
            raise ControlPlaneError(
                self._api_error_message(status_code=response.status_code, detail=message),
                status_code=response.status_code,
            )

        if not _looks_like_tidal_response(payload):
            raise self._unexpected_response_error(path=path, status_code=response.status_code)

        status = payload.get("status")
        if status == "error":
            message = _extract_error_detail(payload) or "API returned an error"
            raise ControlPlaneError(
                self._api_error_message(status_code=response.status_code, detail=message),
                status_code=response.status_code,
            )
        return payload

    def get_dashboard(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/tidal/dashboard")

    def get_kick_logs(self, *, limit: int, offset: int = 0, status: str | None = None, source: str | None = None, auction: str | None = None) -> dict[str, Any]:
        params = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = status
        if source is not None:
            params["source"] = source
        if auction is not None:
            params["auction"] = auction
        return self.request("GET", "/api/v1/tidal/logs/kicks", params=params)

    def get_scan_logs(self, *, limit: int, offset: int = 0, status: str | None = None) -> dict[str, Any]:
        params = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = status
        return self.request("GET", "/api/v1/tidal/logs/scans", params=params)

    def get_run_detail(self, run_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/tidal/logs/runs/{run_id}")

    def get_kick_auctionscan(self, kick_id: int) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/tidal/kicks/{kick_id}/auctionscan")

    def get_deploy_defaults(self, strategy: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/tidal/strategies/{strategy}/deploy-defaults")

    def inspect_kicks(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/v1/tidal/kick/inspect", json=body)

    def prepare_kicks(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/v1/tidal/kick/prepare", json=body)

    def prepare_deploy(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/v1/tidal/auctions/deploy/prepare", json=body)

    def prepare_enable_tokens(self, auction: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/tidal/auctions/{auction}/enable-tokens/prepare", json=body)

    def prepare_settle(self, auction: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/tidal/auctions/{auction}/settle/prepare", json=body)

    def prepare_sweep(self, auction: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/tidal/auctions/{auction}/sweep/prepare", json=body)

    def list_actions(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        operator: str | None = None,
        status: str | None = None,
        action_type: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if operator is not None:
            params["operator"] = operator
        if status is not None:
            params["status"] = status
        if action_type is not None:
            params["action_type"] = action_type
        return self.request("GET", "/api/v1/tidal/actions", params=params)

    def verify_authenticated_access(self) -> None:
        self.list_actions(limit=1)

    def get_action(self, action_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/tidal/actions/{action_id}")

    def report_broadcast(self, action_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/tidal/actions/{action_id}/broadcast", json=body)

    def report_receipt(self, action_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/tidal/actions/{action_id}/receipt", json=body)
