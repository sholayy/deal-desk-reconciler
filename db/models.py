"""
SQLAlchemy Core table definitions for Phase 2 Increment 1.

Design notes the reviewer's ADR review and correction patch required:

- Every tenant-scoped table carries `tenant_id`. The reviewer rejected a
  single-tenant schema even for a demo: once persistence lands, going back
  and sharding across a tenant key is expensive and error-prone.
- Source event uniqueness is enforced by the database via
  `UNIQUE (tenant_id, source, source_event_id)`.
- `reconciliation_runs` carries a secondary `UNIQUE (tenant_id, id)`
  constraint so cross-table composite FKs can reference it. This is the
  substrate that allows `observations` to enforce same-tenant linkage at
  the DB level (correction patch issue 3).
- `observations.reconciliation_run_id` participates in a composite
  foreign key `(tenant_id, reconciliation_run_id)` -> reconciliation_runs
  `(tenant_id, id)`. The result: an observation for Tenant A cannot
  reference a run belonging to Tenant B. Merely sharing a column is not
  enforcement; the FK is.
- Every timestamp is `timestamptz`. Naive datetimes are rejected at the
  application write boundary in `db/writes.py`, since PostgreSQL does
  accept naive values and silently assigns them UTC. The column type is
  the storage choice; validation is the application's job.
- JSONB payloads. Event and snapshot shapes evolve; schema migrations for
  every new field would be unusable.
- `observations` additionally carries `source_record_id` and
  `source_version` per ADR 0004 so snapshots can be ordered by the source
  system's own versioning when multiple snapshots exist for the same
  opportunity.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID


# Shared metadata for all tables in this module. Alembic reads this.
metadata = MetaData()


# -------------------------------------------------------------------
# reconciliation_runs — execution metadata
# -------------------------------------------------------------------

reconciliation_runs = Table(
    "reconciliation_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column(
        "status",
        String(16),
        nullable=False,
        comment="One of: started, completed, failed",
    ),
    Column(
        "input_cutoff",
        DateTime(timezone=True),
        nullable=False,
        comment="Business time the run evaluated against (simulated_now).",
    ),
    Column(
        "ruleset_version",
        String(64),
        nullable=False,
        comment="Version or hash of contract.yaml used for this run.",
    ),
    Column("notes", Text, nullable=True),
    CheckConstraint(
        "status IN ('started', 'completed', 'failed')",
        name="reconciliation_runs_status_check",
    ),
    # Secondary unique constraint so composite FKs can reference
    # (tenant_id, id). Required by the observations.reconciliation_run_id
    # composite FK.
    UniqueConstraint(
        "tenant_id", "id",
        name="uq_reconciliation_runs_tenant_id",
    ),
    Index("ix_reconciliation_runs_tenant_started", "tenant_id", "started_at"),
)


# -------------------------------------------------------------------
# source_events — durable, deduplicated external events
# -------------------------------------------------------------------

source_events = Table(
    "source_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column(
        "source",
        String(32),
        nullable=False,
        comment="salesforce, mission_control, manual, etc.",
    ),
    Column(
        "source_event_id",
        String(128),
        nullable=False,
        comment="The external event identifier provided by the source system.",
    ),
    Column("event_type", String(128), nullable=False),
    Column(
        "occurred_at",
        DateTime(timezone=True),
        nullable=False,
        comment="Business time: when the event happened in the source system.",
    ),
    Column(
        "received_at",
        DateTime(timezone=True),
        nullable=False,
        comment="Database receipt time. Insert time, not wall clock on retries.",
    ),
    Column("payload", JSONB, nullable=False),
    Column(
        "schema_version",
        String(32),
        nullable=False,
        comment="Semver of the event payload schema.",
    ),
    UniqueConstraint(
        "tenant_id", "source", "source_event_id",
        name="uq_source_events_tenant_source_event",
    ),
    Index(
        "ix_source_events_tenant_type_occurred",
        "tenant_id", "event_type", "occurred_at",
    ),
)


# -------------------------------------------------------------------
# observations — Salesforce and delivery snapshots at a point in time
# -------------------------------------------------------------------

observations = Table(
    "observations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("source", String(32), nullable=False),
    Column("opportunity_id", String(64), nullable=False),
    Column(
        "observed_at",
        DateTime(timezone=True),
        nullable=False,
        comment="Business time the record state reflects.",
    ),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        comment="Database insert time.",
    ),
    Column("snapshot", JSONB, nullable=False),
    Column(
        "source_record_id",
        String(128),
        nullable=True,
        comment="Native identifier of the record on the source system.",
    ),
    Column(
        "source_version",
        String(64),
        nullable=True,
        comment="Source-system version/etag for snapshot ordering.",
    ),
    # Nullable: an observation may stand outside a formal reconciliation
    # run (webhook-driven event intake in a later increment).
    Column("reconciliation_run_id", UUID(as_uuid=True), nullable=True),
    # Composite FK enforces same-tenant linkage at the DB layer.
    # `tenant_id` here must match `tenant_id` on the referenced run.
    # PostgreSQL 16 column-specific SET NULL keeps the non-nullable tenant_id
    # intact when a run is deleted; only reconciliation_run_id is unlinked.
    ForeignKeyConstraint(
        ["tenant_id", "reconciliation_run_id"],
        ["reconciliation_runs.tenant_id", "reconciliation_runs.id"],
        ondelete="SET NULL (reconciliation_run_id)",
        name="fk_observations_run_same_tenant",
    ),
    Index(
        "ix_observations_tenant_opp_observed",
        "tenant_id", "opportunity_id", "observed_at",
    ),
    Index(
        "ix_observations_run",
        "reconciliation_run_id",
    ),
)
