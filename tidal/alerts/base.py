"""Destination-aware external alert transport types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AlertMessage:
    delivery_key: str
    occurrence_id: str
    severity: str
    title: str
    summary: str
    retry_at: str | None
    links: tuple[str, ...]


class AlertSink(Protocol):
    @property
    def destination_codes(self) -> tuple[str, ...]: ...

    async def send(self, destination_code: str, message: AlertMessage) -> None: ...


class NullAlertSink:
    destination_codes: tuple[str, ...] = ()

    async def send(self, destination_code: str, message: AlertMessage) -> None:
        del destination_code, message
