# ADR 0004: PostgreSQL event store, containerized local development, schema migrations via Alembic

**Status:** Proposed — revised for incident lifecycle and reliable event processing
**Date:** 2026-11-08
**Decision owner:** Deal Desk Engineering

## Context

Phase 1 produced a pure business-logic layer (`rules.py`, `handoff/eligibility.py`) wrapped by a FastAPI surface (`api/main.py`) that classifies discrepancies in-memory from CSV uploads or JSON payloads. ADR 0001 split event validation from reconciliation into two endpoints. ADR 0003 added a synthetic dataset generator so later phases have realistic volume to operate on.

The gap Phase 2 closes is persistence. Without a durable store the system cannot:

- Track whether a discrepancy was observed previously, which is the input a weekly recurrence rollup needs
- Separate observation events (what SF and MC said about an opportunity at a point in time) from resolution events (the approval was revoked, the stage changed, the handoff was submitted)
- Reconstruct the state of an opportunity at a past point in time, which is the mechanism behind "this incident has recurred three weeks running"
- Survive a restart with any state intact

Phase 3 (orchestration, retry, dead-letter queue) and Phase 4 (metrics scorecards, recurrence-driven case study) both depend on this store existing. Building either before persistence means rewriting them against the store later. We build the store first.

## Decision

Add a PostgreSQL 16 event store, run locally via Docker Compose, with schema migrations managed by Alembic.

Three design choices are load-bearing and deserve direct statement:

1. **Append-only evidence with a mutable, rebuildable incident projection.** Source events, observations, classification decisions and resolution events preserve historical evidence; an `incidents` table maintains query-friendly operational state. `incident_occurrences` links each sighting to its incident episode. A logged resolution is not proof of disappearance or successful prevention. No full event sourcing/CQRS requirement.

2. **JSONB for event payloads, columnar only for fields we query on.** Event types evolve. A commercial-approval-approved payload in six months may carry fields we have not thought of. Storing the payload as JSONB with indexed columns for `opportunity_id`, `occurred_at`, and `event_type` gives us queryability where it matters and flexibility where it does not.

3. **Separate stateless reconciliation from stored-state processing.** Keep `/reconcile` with supplied SF/MC records deterministic and backward compatible. A distinct stored-state operation selects consistent observations and persists a reconciliation run plus decisions transactionally. Do not quietly change `/reconcile` semantics based on optional flags. The event-intake endpoint acknowledges durable receipt, not completed handoff.

## Alternatives considered

### Alternative 1: SQLite with WAL mode

Rejected. SQLite would ship with zero install (Python standard library) and is defensible for a portfolio piece as "the lightweight choice." It is the wrong call here because:

- The portfolio's job is to demonstrate engineering judgment, not corner-cutting. A reviewer evaluating RevOps Manager or GTM Engineer fit wants to see the production-shaped choice.
- SQLite's JSON support is functional but weaker than Postgres JSONB. We would be fighting the tool as event schemas evolve.
- Concurrent writes are where SQLite falls over, and Phase 3 orchestration (worker process plus API) writes concurrently.

SQLite may later be offered as a documented lightweight mode. It is not yet implemented and must not be promised in the README.

### Alternative 2: MongoDB or DynamoDB

Rejected. Document stores are a natural fit for polymorphic event payloads but discard relational integrity. Observations reference events. Discrepancies reference observations. We want foreign keys enforced at the database level, not reimplemented in application code. Postgres gives us both relational constraints and JSONB flexibility in one tool.

### Alternative 3: File-based append log (JSON Lines or Parquet)

Rejected. The query patterns Phase 3 and Phase 4 need (open-discrepancy counts by field, recurrence windows by opportunity, DLQ depth by event type) demand indexed access. Rebuilding indexed access over a file log is reimplementing a database badly.

### Alternative 4: Full event sourcing with CQRS and projections

Rejected on the same grounds as in ADR 0001. Full event sourcing means every state change is an event, state is a projection, and projections are rebuildable by replaying the log. Powerful, and overkill for the invariants this project needs to hold. The append-only observations table plus the resolution-events table plus the derived discrepancies table captures the operational insight ("recurring incident," "time to resolution," "SLA breach") without demanding projection rebuilds.

### Alternative 5: Postgres via native install (Homebrew, apt)

Rejected. A reviewer who wants to run the system locally should not have to `brew install postgresql`, create a role, configure `pg_hba.conf`, and start a service. Docker Compose means `docker compose up` and the entire stack is running. Reproducibility is the signal; native install adds setup tax with no architectural payoff.

### Alternative 6: SQLAlchemy ORM over raw SQL or SQLAlchemy Core

The project will use SQLAlchemy Core (expression language) plus Alembic, not the full ORM. The ORM adds session machinery, lazy loading, and identity-map behavior that this project does not need. Core gives us composable SQL expressions in Python with type checking, integrates natively with Alembic, and stays close to the SQL a reviewer can read directly. Raw psycopg2 is defensible too; Core is the better fit because migrations and schema definitions share a vocabulary.

## Proposed schema and invariants (design contract, not yet a migration)

All business identities are scoped by `tenant_id` / source organization. Database timestamps are timezone-aware. `occurred_at`, `received_at`, and `observed_at` have distinct meanings. Event payloads are validated by versioned Pydantic schemas before being eligible for processing; JSONB alone does not enforce event shape.

| Table | Core columns and constraints | Mutability |
| --- | --- | --- |
| `source_events` | `id` UUID PK, `tenant_id`, `source`, `source_event_id`, `event_type`, `schema_version`, `occurred_at`, `received_at`, `payload` JSONB; unique `(tenant_id, source, source_event_id)` | Insert-only |
| `observations` | `id` UUID PK, `tenant_id`, `source`, `opportunity_id`, `source_record_id`, `source_version`, `observed_at`, `collected_at`, `snapshot` JSONB, optional `source_event_id`; indexed by `(tenant_id, opportunity_id, source, observed_at)` | Insert-only |
| `reconciliation_runs` | `id` UUID PK, `tenant_id`, `started_at`, `completed_at`, `status`, `ruleset_version`, `input_cutoff`, `error_summary` | Lifecycle metadata updated |
| `classification_decisions` | `id` UUID PK, `run_id` FK, `tenant_id`, `opportunity_id`, `concept`, `missing_direction`, `bucket`, `action`, `rule_applied`, `ruleset_version`, `sf_observation_id` FK nullable, `mc_observation_id` FK nullable, `decided_at`, `reason`, `values` JSONB; unique natural decision identity within a run | Insert-only |
| `resolution_events` | `id` UUID PK, `tenant_id`, `incident_id` FK, `actor`, `occurred_at`, `recorded_at`, `root_cause`, `corrective_action`, `verification_plan`, `payload` JSONB | Insert-only |
| `incidents` | `id` UUID PK, `tenant_id`, `incident_key`, `episode_number`, `first_seen_at`, `last_seen_at`, `status`, `occurrence_count`, `last_decision_id` FK; unique `(tenant_id, incident_key, episode_number)` | Rebuildable projection |
| `incident_occurrences` | `incident_id` FK, `decision_id` FK; unique `(incident_id, decision_id)` | Insert-only |
| `event_processing` | `event_id` FK unique, `status`, `attempt_count`, `next_attempt_at`, `lease_until`, `last_error`, `processed_at` | Mutable processing state |

**Identity rules:** An `incident_key` identifies one business discrepancy (including missing-record direction and relevant approved commitment version). An `episode_number` distinguishes a new occurrence after verified closure from consecutive sightings of the same unresolved problem. The classification/incident linkage must not rely on `first_seen_at` alone. Definition of episode closure must be tested on observation sequences; a manual resolution event does not automatically close an episode.

**Handoff uniqueness:** The durable workflow later needs a unique business commitment key such as `(tenant_id, source_org_id, opportunity_id, approved_contract_version, handoff_type)` and an explicit amendment/rework policy. Python eligibility checks are advisory without a database uniqueness constraint under concurrency. Creation must be idempotent at the external delivery boundary as well.

**Snapshot consistency:** Reconciliation runs identify the source observations and rule version used. Missing observations caused by input failure must not be interpreted as missing business records. Never calculate “no discrepancies today” from a failed or partial run.

**Preservation:** Projection rebuilds must not delete historical events, observations, decisions, or resolutions. Database migrations do not re-label old decisions under a new ruleset; a reclassification is a new decision with its own rule version.

## Event ingestion and recovery boundary

- Authenticate and validate event schema; insert source event and initial processing row in **one transaction**. Respond with `202 Accepted` only after commit, never implying delivery work completed.
- Uniqueness on `(tenant_id, source, source_event_id)` handles event replay beyond a 24-hour TTL. Same identity with conflicting payload is a security/integrity incident, not a silent upsert.
- Processing claims a persisted event under a transaction/lease. Transient errors retry with bounded exponential backoff. Exhausted attempts move to a durable failed status for replay or operator intervention.
- Crash after an external call but before marking completion must not create duplicate projects: use a stable business-action idempotency key and query/reconcile remote state before retrying.
- API classification remains synchronous for bounded, side-effect-free requests during Phase 2. Worker-based execution is introduced before asynchronous side-effecting flows, without moving policy into the worker.
- Do not accept caller-supplied empty “history” as verified absence when authorizing external actions. Production authorization reads persisted source state and explicitly fails closed when unavailable.

## Testing and operational readiness

- Start a disposable PostgreSQL 16 container; run `alembic upgrade head` and schema/constraint tests.
- Integration tests use **real committed transactions** across connections for deduplication, concurrent handoff claims, worker crash/restart, and replay. Per-test rollbacks remain useful for isolated repository tests but are not sufficient alone.
- Add schema-version compatibility tests, invalid/naive-timestamp tests, missing-source-snapshot tests, and incident episodes (observe → resolve → absent → recur) tests.
- Docker named volumes are persistence, **not backups**. Document backup/restore commands, retention policy, access controls, and a tested restore procedure before any production deployment.
- Keep `.env` untracked; avoid real customer data or credentials in GitHub Pages, logs, test fixtures, and Slack payloads.
- `docker compose up` must not imply readiness until database health checks pass and migrations have succeeded. Tests should be reproducible from a clean clone.

## Phased implementation scope

1. **Increment A:** Postgres service, Alembic, tenant/source identifiers, source events, observations and run metadata; constraints plus repeatable tests.
2. **Increment B:** Immutable classification decisions and resolution events; incident projections/occurrences and lifecycle tests.
3. **Increment C:** Durable processing inbox, recovery, idempotent authorized handoff execution, concurrency and failure-injection tests.

No production write-back and no silent financial corrections in these increments. The existing CSV/demo path must keep working.

## Consequences

### Positive

- Persistence across sessions. The system has state beyond one process lifetime.
- Queryable history. Recurrence detection becomes a SQL query, not a bespoke in-memory algorithm.
- Referential integrity enforced at the DB. Decisions and incidents reference recorded evidence.
- JSONB lets event payloads evolve without schema migrations for every new event type.
- Reviewer runs the whole stack with `docker compose up` and `pytest`. No native Postgres required.
- Phase 3 orchestration has a durable target. Phase 4 metrics have real history to compute against.

### Negative

- Docker Desktop required on any machine running the system. Documented in the README; SQLite fallback noted but not built.
- More moving parts than the Phase 1 system. The README grows a "starting the stack" section.
- Test speed. Database-backed tests are slower than in-memory. Use per-test rollback for isolated checks and committed multi-connection integration tests for concurrency and recovery.
- Backup and seeding become operational concerns. For local dev this is handled by Alembic migrations plus a seed script that pipes the synthetic generator's output into the observations table.

### Operational

- `docker-compose.yml` at the repo root defines a `postgres:16` service with a named volume `deal_desk_pgdata`.
- Environment variables in `.env.example`. Actual `.env` is gitignored.
- Alembic lives at `alembic/` with `versions/` holding migration scripts. One migration per schema change, each with `upgrade()` and `downgrade()` functions.
- Test database is a separate `deal_desk_test` database, provisioned at test session startup by `conftest.py`.
- README gains a "Running the stack locally" section. Target: a reviewer clones the repo, runs `docker compose up`, `alembic upgrade head`, `pytest`, and sees green tests.

## Decisions and remaining open questions

- **Decided:** PostgreSQL 16, Docker Compose, SQLAlchemy Core, Alembic, single validated JSONB event table, append-only evidence plus rebuildable mutable incident projections.
- **Decided:** API-only synchronous classification is acceptable for bounded Phase 2 calls; durable worker semantics are required for future asynchronous actions.
- **Decided:** Alembic autogenerate may produce an initial draft, but every migration is reviewed, tested and version controlled.
- **Open:** Exact episode-closing policy and evidence threshold for a verified fix; finalize with lifecycle fixtures before the incident projection migration.
- **Open:** Cross-tenant authentication and privacy/retention policy for a real adopter; demo remains synthetic-data-only.
- **Open:** Approved contract version and amendment workflow semantics; must be settled before external handoff creation.

## References

- `docs/decisions/0001-split-event-validation-from-reconciliation.md`: the architectural split this store serves.
- `docs/decisions/0002-fastapi-wrapper-two-endpoints.md`: the API the store integrates with.
- `docs/decisions/0003-synthetic-dataset-generator.md`: the data source that seeds the observations table for local demos and tests.
- Postgres JSONB documentation: https://www.postgresql.org/docs/16/datatype-json.html
- Alembic documentation: https://alembic.sqlalchemy.org/en/latest/
