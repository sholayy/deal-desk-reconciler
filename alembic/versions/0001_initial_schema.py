"""Initial schema: source_events, observations, reconciliation_runs

Revision ID: 0001
Revises:
Create Date: 2026-11-10

Phase 2 Increment 1. Three tables forming the persistence foundation
the reviewer-revised ADR 0004 specified:

- reconciliation_runs: execution metadata (started/completed/failed,
  input_cutoff, ruleset_version) preserving the run_id concept. Carries
  a secondary UNIQUE (tenant_id, id) constraint to serve as a target for
  tenant-scoped composite foreign keys.
- source_events: durable deduplicated external events with the reviewer-
  required uniqueness constraint on (tenant_id, source, source_event_id).
- observations: point-in-time snapshots, optionally tied to a run via a
  composite (tenant_id, reconciliation_run_id) foreign key so cross-
  tenant references are rejected at the DB layer. Also carries
  source_record_id and source_version per ADR 0004.

Classification decisions, resolution events, and incident projections
belong to Increment 2 and are not created here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("input_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ruleset_version", sa.String(64), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'completed', 'failed')",
            name="reconciliation_runs_status_check",
        ),
        sa.UniqueConstraint(
            "tenant_id", "id",
            name="uq_reconciliation_runs_tenant_id",
        ),
    )
    op.create_index(
        "ix_reconciliation_runs_tenant_started",
        "reconciliation_runs",
        ["tenant_id", "started_at"],
    )

    op.create_table(
        "source_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "source", "source_event_id",
            name="uq_source_events_tenant_source_event",
        ),
    )
    op.create_index(
        "ix_source_events_tenant_type_occurred",
        "source_events",
        ["tenant_id", "event_type", "occurred_at"],
    )

    op.create_table(
        "observations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("opportunity_id", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("source_record_id", sa.String(128), nullable=True),
        sa.Column("source_version", sa.String(64), nullable=True),
        sa.Column("reconciliation_run_id", UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "reconciliation_run_id"],
            ["reconciliation_runs.tenant_id", "reconciliation_runs.id"],
            ondelete="SET NULL (reconciliation_run_id)",
            name="fk_observations_run_same_tenant",
        ),
    )
    op.create_index(
        "ix_observations_tenant_opp_observed",
        "observations",
        ["tenant_id", "opportunity_id", "observed_at"],
    )
    op.create_index("ix_observations_run", "observations", ["reconciliation_run_id"])


def downgrade() -> None:
    op.drop_index("ix_observations_run", table_name="observations")
    op.drop_index(
        "ix_observations_tenant_opp_observed", table_name="observations",
    )
    op.drop_table("observations")

    op.drop_index(
        "ix_source_events_tenant_type_occurred", table_name="source_events",
    )
    op.drop_table("source_events")

    op.drop_index(
        "ix_reconciliation_runs_tenant_started",
        table_name="reconciliation_runs",
    )
    op.drop_table("reconciliation_runs")
