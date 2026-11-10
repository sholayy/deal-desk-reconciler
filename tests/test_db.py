"""
Persistence tests for Phase 2 Increment 1 (post reviewer correction patch).

Acceptance bar from the reviewer's checklist:

1. Migration applies cleanly to an empty test database.
2. Schema constraints hold (status CHECK, uniqueness, FKs).
3. Duplicate source events are rejected by the DB.
4. Cross-tenant observation/run references are rejected by the DB.
5. Naive application timestamps are rejected at the write boundary.
6. Committed records survive new connections.
7. Database tests cannot accidentally target the development database.
   (Enforced by conftest's TEST_DATABASE_URL guard.)
8. Migration downgrade and upgrade succeed.

The "restart recovery" item (records survive a container restart) is
documented in the README as a manual verification step: pytest cannot
easily orchestrate a `docker compose restart` mid-run.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from db.models import observations, reconciliation_runs, source_events
from db.writes import (
    NaiveDatetimeError,
    insert_observation,
    insert_reconciliation_run,
    insert_source_event,
)


TENANT = "test-tenant"
NOW = datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc)


# -------------------------------------------------------------------
# Round trips via the write helpers (which validate timestamps)
# -------------------------------------------------------------------

def test_insert_source_event_round_trip(db_session):
    event_id = insert_source_event(
        db_session,
        tenant_id=TENANT,
        source="salesforce",
        source_event_id=f"evt-{uuid.uuid4()}",
        event_type="opportunity.approval.approved",
        occurred_at=NOW,
        payload={"opportunity_id": "OPP-1000"},
        schema_version="1.0.0",
    )
    db_session.flush()
    result = db_session.execute(
        select(source_events).where(source_events.c.id == event_id)
    ).mappings().one()
    assert result["tenant_id"] == TENANT
    assert result["payload"]["opportunity_id"] == "OPP-1000"
    assert result["occurred_at"].tzinfo is not None


def test_insert_reconciliation_run_round_trip(db_session):
    run_id = insert_reconciliation_run(
        db_session,
        tenant_id=TENANT,
        started_at=NOW,
        status="completed",
        input_cutoff=NOW,
        ruleset_version="v1.0.0",
        completed_at=NOW,
    )
    db_session.flush()
    result = db_session.execute(
        select(reconciliation_runs).where(reconciliation_runs.c.id == run_id)
    ).mappings().one()
    assert result["status"] == "completed"
    assert result["ruleset_version"] == "v1.0.0"


# -------------------------------------------------------------------
# Timestamp validation at the write boundary
# -------------------------------------------------------------------

def test_naive_datetime_rejected_by_write_helper(db_session):
    """
    PostgreSQL silently assumes UTC for naive datetimes. The write helper
    layer must reject them before they reach the DB. This test documents
    the contract and is the reviewer's required proof of enforcement.
    """
    naive = datetime(2026, 10, 28, 13, 30)  # no tzinfo
    with pytest.raises(NaiveDatetimeError, match="occurred_at"):
        insert_source_event(
            db_session,
            tenant_id=TENANT,
            source="salesforce",
            source_event_id="x",
            event_type="t",
            occurred_at=naive,
            payload={},
            schema_version="1",
        )


def test_naive_datetime_rejected_on_run(db_session):
    naive = datetime(2026, 10, 28, 13, 30)
    with pytest.raises(NaiveDatetimeError, match="started_at"):
        insert_reconciliation_run(
            db_session,
            tenant_id=TENANT,
            started_at=naive,
            status="started",
            input_cutoff=NOW,
            ruleset_version="v1",
        )


# -------------------------------------------------------------------
# Schema constraints (enforced at the DB layer)
# -------------------------------------------------------------------

def test_reconciliation_run_status_check_rejects_invalid(db_session):
    with pytest.raises(IntegrityError):
        db_session.execute(
            insert(reconciliation_runs).values(
                id=uuid.uuid4(),
                tenant_id=TENANT,
                started_at=NOW,
                status="probably_fine",
                input_cutoff=NOW,
                ruleset_version="v1",
            )
        )
        db_session.flush()


def test_duplicate_source_event_rejected(db_session):
    """
    The unique constraint on (tenant_id, source, source_event_id) is the
    reviewer-required idempotency guarantee. A replay (same tenant, same
    source, same external event id) must fail at the DB layer.
    """
    insert_source_event(
        db_session, tenant_id=TENANT, source="salesforce",
        source_event_id="shared-id", event_type="t", occurred_at=NOW,
        payload={}, schema_version="1",
    )
    db_session.flush()
    with pytest.raises(IntegrityError):
        insert_source_event(
            db_session, tenant_id=TENANT, source="salesforce",
            source_event_id="shared-id", event_type="t", occurred_at=NOW,
            payload={}, schema_version="1",
        )
        db_session.flush()


def test_different_tenant_same_event_id_allowed(db_session):
    """Uniqueness is per-tenant. Two tenants can coexist on the same id."""
    insert_source_event(
        db_session, tenant_id="tenant-a", source="salesforce",
        source_event_id="E-1", event_type="t", occurred_at=NOW,
        payload={}, schema_version="1",
    )
    insert_source_event(
        db_session, tenant_id="tenant-b", source="salesforce",
        source_event_id="E-1", event_type="t", occurred_at=NOW,
        payload={}, schema_version="1",
    )
    db_session.flush()  # no error expected


# -------------------------------------------------------------------
# Composite FK: same-tenant linkage between observations and runs
# -------------------------------------------------------------------

def test_same_tenant_observation_references_run(db_session):
    """An observation belonging to Tenant A points at a run of Tenant A."""
    run_id = insert_reconciliation_run(
        db_session, tenant_id=TENANT, started_at=NOW, status="started",
        input_cutoff=NOW, ruleset_version="v1",
    )
    obs_id = insert_observation(
        db_session, tenant_id=TENANT, source="salesforce",
        opportunity_id="OPP-1", observed_at=NOW, snapshot={},
        reconciliation_run_id=run_id,
    )
    db_session.flush()
    row = db_session.execute(
        select(observations).where(observations.c.id == obs_id)
    ).mappings().one()
    assert row["reconciliation_run_id"] == run_id


def test_cross_tenant_observation_reference_rejected(db_session):
    """
    The composite FK (tenant_id, reconciliation_run_id) -> runs
    (tenant_id, id) rejects an observation from Tenant B pointing at a
    run from Tenant A. This is the reviewer's correction-patch issue 3
    requirement: tenant isolation at the DB layer, not just in column
    naming.
    """
    run_id = insert_reconciliation_run(
        db_session, tenant_id="tenant-a", started_at=NOW, status="started",
        input_cutoff=NOW, ruleset_version="v1",
    )
    db_session.flush()
    with pytest.raises(IntegrityError):
        insert_observation(
            db_session, tenant_id="tenant-b", source="salesforce",
            opportunity_id="OPP-1", observed_at=NOW, snapshot={},
            reconciliation_run_id=run_id,  # points at tenant-a's run
        )
        db_session.flush()


def test_on_delete_set_null_clears_reconciliation_run_id(db_session):
    """
    Observations are evidence and must survive run deletion. The FK's
    ON DELETE SET NULL clause unlinks the observation but keeps the row.
    """
    run_id = insert_reconciliation_run(
        db_session, tenant_id=TENANT, started_at=NOW, status="completed",
        input_cutoff=NOW, ruleset_version="v1", completed_at=NOW,
    )
    obs_id = insert_observation(
        db_session, tenant_id=TENANT, source="salesforce",
        opportunity_id="OPP-1", observed_at=NOW, snapshot={},
        reconciliation_run_id=run_id,
    )
    db_session.flush()

    db_session.execute(
        reconciliation_runs.delete().where(reconciliation_runs.c.id == run_id)
    )
    db_session.flush()

    row = db_session.execute(
        select(observations).where(observations.c.id == obs_id)
    ).mappings().one()
    assert row["reconciliation_run_id"] is None  # unlinked, not deleted


# -------------------------------------------------------------------
# Persistence across separate connections
# -------------------------------------------------------------------

def test_persistence_across_connections(test_engine):
    """
    Write with one connection and COMMIT. Close it. Open a fresh one.
    Read the data. Confirms the storage layer actually persists beyond a
    single transaction and connection lifetime.

    This test takes the engine fixture directly rather than the
    `db_session` fixture because the fixture rolls back and would hide
    any persistence.
    """
    event_id = uuid.uuid4()
    # Commit on connection 1.
    with test_engine.begin() as conn:
        conn.execute(
            insert(source_events).values(
                id=event_id, tenant_id=TENANT, source="salesforce",
                source_event_id=f"persist-{event_id}",
                event_type="t", occurred_at=NOW, received_at=NOW,
                payload={}, schema_version="1",
            )
        )

    # Read on connection 2.
    try:
        with test_engine.connect() as conn:
            result = conn.execute(
                select(source_events).where(source_events.c.id == event_id)
            ).mappings().one()
            assert result["id"] == event_id
    finally:
        # Clean up so later test runs stay deterministic.
        with test_engine.begin() as conn:
            conn.execute(
                source_events.delete().where(source_events.c.id == event_id)
            )


# -------------------------------------------------------------------
# Migration lifecycle: downgrade and upgrade succeed
# -------------------------------------------------------------------

def test_migration_downgrade_and_upgrade_round_trip(migration_test_url, monkeypatch):
    """Run destructive migration checks only on the dedicated migration DB.

    Alembic env.py deliberately overrides sqlalchemy.url from environment, so
    the test supplies ALEMBIC_DATABASE_URL explicitly. NEVER use the ordinary
    test database or development DATABASE_URL for downgrade tests.
    """
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from sqlalchemy import create_engine, inspect, text

    alembic_ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", migration_test_url)
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("sqlalchemy.url", migration_test_url)

    engine = create_engine(migration_test_url)
    try:
        existing_tables = set(inspect(engine).get_table_names())
        allowed_tables = {"alembic_version", "source_events", "observations", "reconciliation_runs"}
        unexpected = existing_tables - allowed_tables
        assert not unexpected, (
            "Refusing destructive migration test against a DB with other tables: "
            + ", ".join(sorted(unexpected))
        )
        # Even the dedicated DB must not contain business records when a
        # destructive downgrade is attempted.
        with engine.connect() as conn:
            for table_name in sorted(existing_tables & {"source_events", "observations", "reconciliation_runs"}):
                count = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
                assert count == 0, (
                    f"Refusing migration downgrade: {table_name} contains {count} rows"
                )
    finally:
        engine.dispose()

    # Test upgrade from a freshly provisioned database too. After the
    # initial upgrade, verify downgrade and re-upgrade. This DB is exclusive
    # to this test and contains no application data.
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(migration_test_url)
    try:
        assert {"source_events", "observations", "reconciliation_runs"}.issubset(
            set(inspect(engine).get_table_names())
        )
    finally:
        engine.dispose()
