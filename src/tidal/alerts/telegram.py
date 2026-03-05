"""Telegram alert sink."""

from __future__ import annotations

import httpx


class TelegramAlertSink:
    def __init__(self, bot_token: str, chat_id: str):
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    async def send_critical(self, title: str, body: str) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": f"[CRITICAL] {title}\n\n{body}",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(self._url, json=payload)
