# ADR 0006: Evidence-backed classification and incident lifecycle

**Status:** Accepted  
**Based on:** the design arc from revisions 1 to 5, consolidated after the revision-5 review found three PostgreSQL specification errors. The earlier revisions remain in git history (last version at commit `016e49789231b6c214c79ede04c6569a3b0d6443`).  
**Decision owner:** Deal Desk Engineering  
**Scope:** Increment 2a classification evidence; Increment 2b incident projections

## Context

ADR 0004 established PostgreSQL as a durable record of source observations and reconciliation runs. The reconciler presently emits discrepancy rows, but **the absence of a discrepancy does not establish agreement**: missing or unparseable values can be skipped. An auditable lifecycle must distinguish a detected discrepancy, a human claim of resolution, an observed correction, independent verification and sustained absence. It must also distinguish repeated sightings from a new incident episode.

The key business requirement is that every claim about a resolved incident must have **positive, traceable evidence**. The system must not silently interpret an incomplete run, missing source record, or unevaluated field as a clean result.

## Decision

1. **Persist explicit evaluation evidence.** For each in-scope `(run, opportunity, concept)` pair, persist one expected-evaluation row and exactly one actual evaluation before the run can complete. Outcomes for comparable field concepts: `MATCH`, `MISMATCH`, `INSUFFICIENT_EVIDENCE`, `NOT_EVALUATED`. Record-presence uses `BOTH_PRESENT` and `ONE_SIDE` (with structured missing direction). Policy-excluded concepts are `NOT_EVALUATED`, never `MATCH`.
2. **Persist classification decisions separately.** A decision is the classified/actionable interpretation of a `MISMATCH` or `ONE_SIDE` evaluation. It references the underlying evaluation and the ruleset used. `MATCH` and unevaluated outcomes never generate discrepancy decisions.
3. **Keep evidence append-only; project incidents.** Runs, observations, expected evaluations, evaluations, decisions and resolution events retain history. `incidents` and `incident_occurrences` are rebuildable operational projections.
4. **Close an episode only on positive evidence.** For field/contract-gap incidents, a qualified `MATCH` advances closure. For missing-record incidents, a qualified `BOTH_PRESENT` on the same opportunity and run advances closure. One qualified clean evaluation closes an episode by default; three consecutive qualified clean evaluations establish `correction_sustained` only when a resolution claim exists. These thresholds are explicitly versioned policy, not proof of prevention.
5. **Keep claims distinct from observations.** An operator's resolution claim cannot close an incident. Independent verification is a separate human assertion and does not substitute for a qualified clean evaluation. `prevention_validated` is out of scope until subsequent control execution can actually be observed.
6. **Use two transactions for each persisted run.** Transaction A durably records the run, its input identity, manifest, and expected evaluations. Transaction B atomically records observations, evaluations, decisions and completion. A failed or started run provides no closure evidence.
7. **Preserve deterministic episode identity.** The incident's natural key includes the canonical opportunity identity, normalized discrepancy class, and concept (or missing-record direction). Episodes are numbered in time order. Their projection UUID is UUIDv5 from a documented namespace and unambiguous canonical serialization of `(tenant_id, incident_key, episode_number)`.

## Evaluation contract

| Concept type | Outcome | Lifecycle meaning |
| --- | --- | --- |
| Comparable field/contract gap | `MATCH` | Qualifying clean evidence if relevant to the incident key |
| Comparable field/contract gap | `MISMATCH` | Detection; creates a decision, held or actionable |
| Any field | `INSUFFICIENT_EVIDENCE` | Required data missing/invalid; never clean |
| Policy-excluded/inapplicable concept | `NOT_EVALUATED` | Never clean |
| `record_presence` | `BOTH_PRESENT` | Both expected records are linked to the same opportunity in the run |
| `record_presence` | `ONE_SIDE` | Missing-record detection with structured `missing_from` |

The manifest defines which opportunities the run covers. The ruleset manifest defines which concepts are expected. Their explicit Cartesian product is recorded as expected-evaluation rows. An absent opportunity creates **no** positive evidence; a failed run is ignored by the lifecycle projection.

## Data model and enforceable constraints

**Names below describe logical tables and essential keys; migrations must match the existing Increment 1 schema.** All tenant-scoped foreign keys include `tenant_id`. Times are `timestamptz`; write helpers reject naive datetimes.

| Table | Required structure |
| --- | --- |
| `reconciliation_runs` (existing) | `tenant_id`, `id`, `input_cutoff`, `ruleset_version`, `status`, source input SHA-256 values, `expected_evaluation_count`, start/completion timestamps. Add qualifying UNIQUE keys `(tenant_id, id, ruleset_version)` and `(tenant_id, id, input_cutoff, ruleset_version)` for the respective FKs. |
| `run_input_manifest` | PK `(tenant_id, run_id, opportunity_id)`; booleans for source presence; FK to run. |
| `ruleset_concepts` | PK `(ruleset_version, concept)`; append a new set when the ruleset changes. Seed deterministically from a **pinned** ruleset artifact, not whatever mutable YAML happens to be on disk during migration execution. |
| `run_expected_evaluations` | PK `(tenant_id, run_id, opportunity_id, concept)`; FK to manifest; FK `(tenant_id, run_id, ruleset_version)` to matching UNIQUE run key; FK `(ruleset_version, concept)` to `ruleset_concepts`. |
| `observations` (existing) | Add source input hash and a suitable UNIQUE key for `(tenant_id, run_id, opportunity_id, source)` **where the business model guarantees one observation per source per opportunity per run**; otherwise model distinct observation revisions explicitly. |
| `concept_evaluations` | UUID PK; UNIQUE `(tenant_id, run_id, opportunity_id, concept)`; FK to expected-evaluation tuple; `outcome`, `evaluated_at`, `detail` JSONB; `sf_observation_id` and `mc_observation_id` nullable. |
| `classification_decisions` | UUID PK, FK to evaluation (including tenant/run/opportunity/concept scope), `decided_at`, `ruleset_version`, `incident_key`, bucket, action, reason; UNIQUE `(tenant_id, run_id, incident_key)`; FK `(tenant_id, run_id, decided_at, ruleset_version)` to corresponding UNIQUE run key. |
| `resolution_events` | Append-only claim or independent-verification event, tenant/key/actor/time; independent verification refers to a specific claim and uses a different actor. |
| `incidents`, `incident_occurrences` | Rebuildable episode projection with stable episode number/ID, detection times, claim/verification references, clean count, closure/sustain times, policy version, and occurrences linked uniquely to decisions. |

### Observation reference strategy

Use observation **IDs**, not source literals embedded in foreign keys. To prove identity, add a suitable UNIQUE key on observations such as `(id, tenant_id, reconciliation_run_id, opportunity_id, source)` and composite FK referencing columns on the evaluation, including its own tenant, run and opportunity identifiers plus source discriminator.

PostgreSQL cannot embed a string literal in a foreign key. A discriminator can be a generated non-null source column (e.g. `'salesforce'`) **if needed**, while each observation ID remains nullable. With `MATCH SIMPLE`, a null observation ID makes that particular FK inapplicable; enforce outcome-dependent presence through separate checks/write validation. Do **not** require every referencing column to become NULL together: tenant, run and opportunity are required for the evaluation itself.

Database constraints prove the referred observation's identity. **Business comparison validity** (`MATCH`, `MISMATCH`, etc.) still requires a pure evaluator and validated write path. Add negative tests for wrong source, tenant, run and opportunity.

### Completeness and transaction integrity

Transaction A:

1. Read and validate both source inputs once; compute SHA-256 over their original bytes and retain those bytes in the process.
2. Insert a `started` run, input hashes and explicit manifest rows.
3. Insert expected evaluations for every manifest opportunity and every concept in the pinned ruleset.
4. Set `expected_evaluation_count` to the actual expected-row count, then commit.

Transaction B:

1. Use the same captured bytes; rehash at the trusted write boundary and compare with the run's recorded hashes.
2. Insert observations, evaluations and classification decisions.
3. Mark the run `completed` **only if** every expected evaluation has one actual row. This follows from (a) the evaluation-to-expected FK, (b) evaluation uniqueness, and (c) equality between the actual and expected row counts, verified by the completion operation in the same transaction. Check both counts directly; do not rely on a mutable cached number alone.
4. Commit all evidence and completion atomically. Otherwise roll back Transaction B and leave the run `started` for recovery.

A plain PostgreSQL `CHECK` cannot compare the run's count column to an aggregate of another table. Enforce completion through a guarded database operation (write helper or database trigger if direct SQL writes must be supported); test bypass attempts accordingly. `CHECK (expected_evaluation_count >= 0)` is valid, but does not by itself prove completeness.

**Crash limitation:** The original source bytes are not persisted in this increment. A crash after Transaction A requires resubmission of the *identical* inputs to resume. Matching hashes verify identity; hashes cannot reconstruct files. Recovery of stalled runs must not mark them completed. Automatic blob retention is deferred.

## Incident lifecycle

For each `incident_key`, sort **completed** runs deterministically by `(input_cutoff, started_at, run_id)` and consume only the matching concept's evaluation evidence.

1. **Detection:** A `MISMATCH` or `ONE_SIDE` decision opens an episode, or adds an occurrence to the currently open episode. A held sighting and later actionable sighting remain in the same episode.
2. **Claim:** A recorded operator resolution annotates the appropriate episode but does not close it. A later detection while that episode remains open can mark the claim `claim_contradicted`; it does not prove a specific preventive fix failed.
3. **Observed correction:** The first qualifying `MATCH` or `BOTH_PRESENT` closes the episode under default policy, records `first_clean_at` and starts monitoring.
4. **Post-closure monitoring:** Continue counting **consecutive qualifying clean evaluations** even though the episode is closed; no-evidence runs do not count. Clarify whether they *pause* or *break* consecutiveness as a documented policy choice (default here: pause, not reset).
5. **Sustained correction:** At the configured threshold (default three), set `correction_sustained_at` if a valid resolution claim exists. Without a claim, record observed correction without attribution; do not claim a validated preventive control.
6. **Recurrence:** A new detection after closure creates episode `n+1`. A detection before closure stays in the original episode. Reappearance during monitoring stops monitoring without granting sustained correction.
7. **Independent verification:** A verification event references an operator claim and has a different actor. It does not override contradictory system observations. An authenticated identity boundary is a later deployment requirement.

`incidents` is a projection, not authoritative evidence. Rebuilds preserve stable UUIDv5 IDs and record the lifecycle policy version used. Changing a lifecycle threshold can restate derived history; published metrics must retain the version and flag restatements.

## Minimal acceptance tests

**Increment 2a: evidence and migration**

- Empty-database upgrade and downgrade work in a disposable PostgreSQL test database, never development.
- A concept not expected for `(run, opportunity)` cannot be evaluated.
- A completed run is rejected when any expected evaluation is missing; duplicate evaluations are rejected.
- Evaluations cannot reference another tenant, run, opportunity or source; optional observation refs work with PostgreSQL FK null semantics.
- `MATCH` cannot be recorded for a policy-excluded concept, nor for a comparison with null/unparseable data.
- A decision cannot reference a `MATCH` evaluation, or disagree with its run's ruleset/time.
- Raw input bytes with changed hashes cannot resume a started run.
- No partial observations/decisions survive a failed Transaction B.

**Increment 2b: lifecycle**

- Detected → claimed → clean → sustained.
- Claimed but still detected (claim contradicted).
- Closed → recurrence, including recurrence before sustained correction.
- Held → actionable remains the same incident key.
- Ruleset changes during an open episode preserve identity while recording rule versions.
- Failed/out-of-scope/insufficient-evidence runs never close an incident.
- Rebuild twice from the same evidence and policy yields the same episodes and identifiers.
- Independent verification cannot be attached by the original claimant.

## Alternatives and consequences

**Rejected:** Inferring correction from no decision row; one mutable incident table without evidence history; treating three clean evaluations as proof a preventive control ran; one transaction that pretends a `started` run survives a crash.

**Accepted cost:** More storage (expected and actual evaluations), more composite constraints, explicit run lifecycle and manual input recovery. At the intended synthetic demo scale, correctness and explainability justify this cost. Avoid adding queue workers, object storage, real-system write-back or preventive-control attestation until later increments.

## Implementation sequence and sign-off

- **2a:** Migration `0002` for evidence tables and integrity, pure `evaluate_concepts`, persisted decisions and real-PostgreSQL adversarial tests.
- **2b:** Resolution history, deterministic episode projector and scenario fixtures.
- **Later:** Worker recovery, external handoff idempotency and verifiable preventive-control execution.

**Release gate:** This ADR is accepted as a design only when the migration has been checked against actual PostgreSQL constraints. A passing Python unit suite or a plausible schema diagram is not sufficient.
