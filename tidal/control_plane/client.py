"""Shared HTTP client for the Tidal control-plane API."""

from __future__ import annotations

from typing import Any

import httpx


class ControlPlaneError(RuntimeError):
    """Raised when the control-plane API returns an error."""


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

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._client.request(method, path, params=params, json=json)
        try:
            payload = response.json()
        except ValueError as exc:
            if not response.is_success:
                message = response.text.strip() or f"API returned HTTP {response.status_code}"
                raise ControlPlaneError(f"API returned {response.status_code}: {message}") from exc
            raise ControlPlaneError(f"API returned invalid JSON ({response.status_code})") from exc

        if not response.is_success:
            message = payload.get("detail") or payload.get("error") or payload.get("message") or response.text
            raise ControlPlaneError(str(message))

        if not isinstance(payload, dict):
            raise ControlPlaneError("API response was not an object")

        status = payload.get("status")
        if status == "error":
            message = payload.get("detail") or payload.get("message") or payload.get("error") or "API returned an error"
            raise ControlPlaneError(str(message))
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

    def get_action(self, action_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/tidal/actions/{action_id}")

    def report_broadcast(self, action_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/tidal/actions/{action_id}/broadcast", json=body)

    def report_receipt(self, action_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/tidal/actions/{action_id}/receipt", json=body)
