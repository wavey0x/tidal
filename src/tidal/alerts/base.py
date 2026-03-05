"""Alert sink interfaces."""

from __future__ import annotations

from typing import Protocol


class AlertSink(Protocol):
    async def send_critical(self, title: str, body: str) -> None:
        """Send a critical alert to an external system."""


class NullAlertSink:
    async def send_critical(self, title: str, body: str) -> None:
        return None
