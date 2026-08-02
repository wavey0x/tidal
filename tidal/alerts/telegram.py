"""Small async Telegram transport for operational alert fan-out."""

from __future__ import annotations

import httpx

from tidal.alerts.base import AlertMessage


class TelegramAlertSink:
    destination_codes = ("admin_alerts", "operations_alerts")

    def __init__(
        self,
        *,
        bot_token: str,
        admin_alert_chat_id: str,
        operations_alert_chat_id: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._bot_token = bot_token
        self._chat_ids = {
            "admin_alerts": admin_alert_chat_id,
            "operations_alerts": operations_alert_chat_id,
        }
        self._timeout_seconds = timeout_seconds

    async def send(self, destination_code: str, message: AlertMessage) -> None:
        chat_id = self._chat_ids.get(destination_code)
        if chat_id is None:
            raise ValueError("unknown alert destination")
        lines = [
            f"[{message.severity.upper()}] {message.title}",
            message.summary,
        ]
        if message.retry_at:
            lines.append(f"Retry at: {message.retry_at}")
        lines.extend(message.links)
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    url,
                    json={"chat_id": chat_id, "text": "\n".join(lines)},
                )
                response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Telegram delivery failed") from exc
