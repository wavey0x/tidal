"""Persisted at-most-once-after-success notification dispatch."""

from __future__ import annotations

import structlog
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tidal.alerts.base import AlertMessage, AlertSink
from tidal.persistence import models
from tidal.security import redact_sensitive_text
from tidal.time import utcnow_iso

logger = structlog.get_logger(__name__)


class AlertDispatcher:
    def __init__(self, *, session, sink: AlertSink) -> None:  # noqa: ANN001
        self.session = session
        self.sink = sink

    async def dispatch(self, messages: tuple[AlertMessage, ...], *, suppress: bool = False) -> None:
        for message in messages:
            # Baseline both supported destinations even when transport is
            # currently unconfigured; later enabling it must not replay old alerts.
            for destination in (("admin_alerts", "operations_alerts") if suppress else self.sink.destination_codes):
                self.session.execute(
                    sqlite_insert(models.alert_deliveries)
                    .values(
                        delivery_key=message.delivery_key,
                        destination=destination,
                        occurrence_id=message.occurrence_id,
                        attempt_count=0,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            models.alert_deliveries.c.delivery_key,
                            models.alert_deliveries.c.destination,
                        ]
                    )
                )
                self.session.commit()
                row = (
                    self.session.execute(
                        models.alert_deliveries.select().where(
                            models.alert_deliveries.c.delivery_key
                            == message.delivery_key,
                            models.alert_deliveries.c.destination == destination,
                        )
                    )
                    .mappings()
                    .one()
                )
                if suppress and row["sent_at"] is None and row["suppressed_at"] is None:
                    self.session.execute(models.alert_deliveries.update().where(
                        models.alert_deliveries.c.delivery_key == message.delivery_key,
                        models.alert_deliveries.c.destination == destination,
                    ).values(suppressed_at=utcnow_iso(), last_error="Suppressed during recovery baseline"))
                    self.session.commit()
                if suppress or row["suppressed_at"] is not None or row["sent_at"] is not None or int(row["attempt_count"]) >= 3:
                    continue

                attempted_at = utcnow_iso()
                self.session.execute(
                    models.alert_deliveries.update()
                    .where(
                        models.alert_deliveries.c.delivery_key == message.delivery_key,
                        models.alert_deliveries.c.destination == destination,
                    )
                    .values(
                        attempt_count=int(row["attempt_count"]) + 1,
                        last_attempt_at=attempted_at,
                        last_error=None,
                    )
                )
                self.session.commit()
                try:
                    await self.sink.send(destination, message)
                except Exception as exc:  # noqa: BLE001
                    sanitized = redact_sensitive_text("alert delivery failed")
                    self.session.execute(
                        models.alert_deliveries.update()
                        .where(
                            models.alert_deliveries.c.delivery_key
                            == message.delivery_key,
                            models.alert_deliveries.c.destination == destination,
                        )
                        .values(last_error=sanitized)
                    )
                    self.session.commit()
                    logger.warning(
                        "alert_delivery_failed",
                        delivery_key=message.delivery_key,
                        destination=destination,
                        error_type=exc.__class__.__name__,
                    )
                    continue
                self.session.execute(
                    models.alert_deliveries.update()
                    .where(
                        models.alert_deliveries.c.delivery_key == message.delivery_key,
                        models.alert_deliveries.c.destination == destination,
                    )
                    .values(sent_at=utcnow_iso(), last_error=None)
                )
                self.session.commit()
