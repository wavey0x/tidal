"""Small async Telegram transport for operational alert fan-out."""

from __future__ import annotations

from html import escape
from urllib.parse import urlparse

import httpx

from tidal.alerts.base import AlertMessage


def _link_label(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    if host == "etherscan.io" or host.endswith(".etherscan.io"):
        return "Transaction"
    if host == "auctionscan.info" or host.endswith(".auctionscan.info"):
        return "Auction"
    return "Details"


class TelegramAlertSink:
    destination_codes = ("admin_alerts", "operations_alerts")

    def __init__(
        self,
        *,
        bot_token: str,
        admin_alert_chat_id: str,
        operations_alert_chat_id: str,
        alerts_url: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._bot_token = bot_token
        self._chat_ids = {
            "admin_alerts": admin_alert_chat_id,
            "operations_alerts": operations_alert_chat_id,
        }
        self._alerts_url = alerts_url
        self._timeout_seconds = timeout_seconds

    async def send(self, destination_code: str, message: AlertMessage) -> None:
        chat_id = self._chat_ids.get(destination_code)
        if chat_id is None:
            raise ValueError("unknown alert destination")
        heading = escape(f"[TIDAL {message.severity.upper()}] {message.title}")
        lines = [f"<b>{heading}</b>", escape(message.summary)]
        if message.retry_at:
            lines.append(f"<b>Retry:</b> {escape(message.retry_at)}")
        links = [
            f'<a href="{escape(self._alerts_url, quote=True)}">Tidal</a>',
            *(
                f'<a href="{escape(link, quote=True)}">{label}</a>'
                for link in message.links
                if (label := _link_label(link)) is not None
            ),
        ]
        lines.extend(("", " · ".join(links)))
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": "\n".join(lines),
                        "parse_mode": "HTML",
                        "link_preview_options": {"is_disabled": True},
                    },
                )
                response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Telegram delivery failed") from exc
