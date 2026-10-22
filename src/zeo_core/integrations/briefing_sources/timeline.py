"""Arrival margin from a validated flight estimate and explicitly stated inputs."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from zeo_core.contracts.ui.widgets import Assumption, DataError, DataRef
from zeo_core.integrations.briefing_sources.http import SourceUnavailableError, utc_now


def unavailable(
    component: str, source: str, fetched_at: datetime, reason: str
) -> DataRef:
    """An empty error payload; callers pass only fixed public reason strings."""
    return DataRef(
        id=component + "-" + uuid4().hex,
        source=source,
        fetched_at=fetched_at.astimezone(UTC).isoformat(),
        as_of=None,
        replay=False,
        payload={},
        error=DataError(reason=str(SourceUnavailableError(reason))),
    ).check(component)


def arrival_timeline(
    flight: DataRef,
    *,
    transfer_minutes: float,
    meeting_time: datetime,
    fetched_at: datetime | None = None,
) -> DataRef:
    fetched_at = fetched_at or utc_now()
    source = (
        "Computed from ADSB.lol position estimate and traveller-stated transfer/meeting"
    )
    try:
        flight = DataRef.model_validate(flight.model_dump()).check("flight_status")
        if (
            flight.error is not None
            or flight.replay
            or isinstance(transfer_minutes, bool)
            or not math.isfinite(transfer_minutes)
            or not 0 <= transfer_minutes <= 1440
            or meeting_time.tzinfo is None
        ):
            raise ValueError
        estimate = flight.payload.get("estimated_arrival")
        if not isinstance(estimate, str):
            raise ValueError
        landing = datetime.fromisoformat(estimate).astimezone(UTC)
        meeting = meeting_time.astimezone(UTC)
        arrival = landing + timedelta(minutes=transfer_minutes)
        margin = (meeting - arrival).total_seconds() / 60
        return DataRef(
            id="timeline-" + uuid4().hex,
            source=source,
            fetched_at=fetched_at.astimezone(UTC).isoformat(),
            as_of=flight.as_of,
            replay=False,
            assumptions=[
                Assumption(
                    label="transfer_minutes", value=transfer_minutes, kind="assumption"
                ),
                Assumption(
                    label="estimated_arrival_from_live_position",
                    value="position + speed + distance, stated transfer and meeting",
                    kind="assumption",
                ),
            ],
            payload={
                "steps": [
                    {"label": "Computed landing", "time": landing.isoformat()},
                    {
                        "label": "Arrival after assumed transfer",
                        "time": arrival.isoformat(),
                    },
                ],
                "transfer_minutes": {"value": transfer_minutes, "kind": "assumption"},
                "meeting_time": meeting.isoformat(),
                "margin_minutes": margin,
            },
        ).check("arrival_timeline")
    except (ValueError, TypeError, OverflowError):
        return unavailable(
            "arrival_timeline", source, fetched_at, "arrival_inputs_unavailable"
        )
