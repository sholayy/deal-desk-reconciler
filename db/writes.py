"""
Validated write helpers for the Phase 2 Increment 1 tables.

The reviewer's correction patch required timezone-aware validation at the
application write boundary. PostgreSQL `timestamptz` does NOT reject naive
datetimes; it silently assumes UTC and stores a timezone-aware value. That
silent conversion masks bugs where the caller forgot to localize. The
helpers here reject naive datetimes before insert and raise `TypeError`
with a specific message naming the field.

These helpers also fill in defaults the schema does not enforce
(`received_at` and `recorded_at` default to `now()` in UTC if the caller
omits them).

Scope decision per the reviewer's additional finding: implementing these
helpers rather than narrowing the increment to schema + connections only.
Callers (Increment 2 projection builder, Increment 3 orchestrator) write
through these functions, not raw INSERT statements, so the validation
cannot be bypassed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session

from db.models import observations, reconciliation_runs, source_events


class NaiveDatetimeError(TypeError):
    """Raised when a caller passes a naive datetime to a write helper."""


def _require_aware(name: str, value: datetime) -> datetime:
    if value.tzinfo is None:
        raise NaiveDatetimeError(
            f"Field '{name}' received a naive datetime. All timestamps "
            "entering the storage layer must be timezone-aware."
        )
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def insert_source_event(
    session: Session,
    *,
    tenant_id: str,
    source: str,
    source_event_id: str,
    event_type: str,
    occurred_at: datetime,
    payload: dict[str, Any],
    schema_version: str,
    received_at: datetime | None = None,
    id: uuid.UUID | None = None,
) -> uuid.UUID:
    """
    Insert one source event. Raises `NaiveDatetimeError` if any datetime
    is naive. Returns the inserted event's id.
    """
    _require_aware("occurred_at", occurred_at)
    if received_at is None:
        received_at = _utc_now()
    else:
        _require_aware("received_at", received_at)
    event_id = id or uuid.uuid4()
    session.execute(
        insert(source_events).values(
            id=event_id,
            tenant_id=tenant_id,
            source=source,
            source_event_id=source_event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            received_at=received_at,
            payload=payload,
            schema_version=schema_version,
        )
    )
    return event_id


def insert_reconciliation_run(
    session: Session,
    *,
    tenant_id: str,
    started_at: datetime,
    status: str,
    input_cutoff: datetime,
    ruleset_version: str,
    completed_at: datetime | None = None,
    notes: str | None = None,
    id: uuid.UUID | None = None,
) -> uuid.UUID:
    """
    Insert one reconciliation run. Raises on naive datetimes or invalid
    status. Returns the inserted run's id.
    """
    _require_aware("started_at", started_at)
    _require_aware("input_cutoff", input_cutoff)
    if completed_at is not None:
        _require_aware("completed_at", completed_at)
    if status not in ("started", "completed", "failed"):
        raise ValueError(
            f"Invalid status '{status}'. Must be started|completed|failed."
        )
    run_id = id or uuid.uuid4()
    session.execute(
        insert(reconciliation_runs).values(
            id=run_id,
            tenant_id=tenant_id,
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            input_cutoff=input_cutoff,
            ruleset_version=ruleset_version,
            notes=notes,
        )
    )
    return run_id


def insert_observation(
    session: Session,
    *,
    tenant_id: str,
    source: str,
    opportunity_id: str,
    observed_at: datetime,
    snapshot: dict[str, Any],
    recorded_at: datetime | None = None,
    reconciliation_run_id: uuid.UUID | None = None,
    source_record_id: str | None = None,
    source_version: str | None = None,
    id: uuid.UUID | None = None,
) -> uuid.UUID:
    """
    Insert one observation. Raises on naive datetimes. Returns the
    inserted observation's id.
    """
    _require_aware("observed_at", observed_at)
    if recorded_at is None:
        recorded_at = _utc_now()
    else:
        _require_aware("recorded_at", recorded_at)
    obs_id = id or uuid.uuid4()
    session.execute(
        insert(observations).values(
            id=obs_id,
            tenant_id=tenant_id,
            source=source,
            opportunity_id=opportunity_id,
            observed_at=observed_at,
            recorded_at=recorded_at,
            snapshot=snapshot,
            source_record_id=source_record_id,
            source_version=source_version,
            reconciliation_run_id=reconciliation_run_id,
        )
    )
    return obs_id
