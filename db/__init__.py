"""
Database layer for the Deal Desk Reconciler.

Three tables form the Phase 2 Increment 1 foundation:

- `source_events`: durable, deduplicated external business events.
  Idempotency is enforced at the DB level via a unique constraint on
  (tenant_id, source, source_event_id). The reviewer's Phase 2 ADR review
  made this non-negotiable.
- `observations`: Salesforce and delivery snapshots at a point in time.
  Each observation may belong to a reconciliation run or stand alone.
- `reconciliation_runs`: execution metadata including input cutoff,
  ruleset version, and completion status. Preserves the run_id concept
  the batch loader already relies on.

See docs/decisions/0004-postgres-event-store-docker-compose.md for the
ADR covering schema shape, migration strategy, and the reasons we chose
PostgreSQL + SQLAlchemy Core + Alembic over the alternatives considered.
"""
