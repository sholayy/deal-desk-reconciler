# ADR 0006: Classification decisions and incident lifecycle

**Status:** Proposed
**Date:** 2026-11-14
**Decision owner:** Deal Desk Engineering
**Closes:** the "Open" item in ADR 0004 (episode-closing policy and the evidence threshold for a verified fix)

## Context

ADR 0004 established the three-layer model: immutable evidence, a rebuildable incident projection, and operational analytics on top. Increment 1 landed the evidence tables for runs, source events and observations. It deliberately left one design question open: when does an incident episode close, and what counts as proof that a fix held?

The reviewer named four states that today's design conflates and that the projection must keep apart:

1. Someone marked the issue resolved.
2. The issue stopped appearing in reconciliation.
3. Someone independently verified the correction.
4. A preventive control held over subsequent relevant activity.

Three facts about the current code shape this ADR:

- `rules.incident_key(entry)` already defines identity as `(opportunity_id, concept, normalized_bucket)`, where `normalized_bucket` collapses `owned_field` and `shared_field` into `field_discrepancy` and folds `below_threshold` (hold) into the identity of the actionable discrepancy it will become. Tests pin this. The persisted key must be the same identity, not a second one.
- The missing-record entry carries direction only inside its `reason` string. `concept` is `None`. "OPP-012 missing from Mission Control" and "OPP-012 missing from Salesforce" therefore hash to the same key. ADR 0004 required the key to carry direction; this ADR adds it.
- `resolve.py` attaches a resolution only to `action == "flag"` entries and enforces `original_observation < resolved_at`. `rules.latest_prior_resolution` enforces `original <= resolved < repeat` before it will call a repeat "still detected after recorded resolution." Those chronology rules are correct and carry forward unchanged.

This ADR is written scenario-first. The schema is derived from what the scenarios need, not the other way around.

## Scenarios the design must answer

Each scenario becomes a lifecycle fixture in `tests/test_incident_lifecycle.py`. Runs are identified by their business-time `input_cutoff`. All runs below are `completed` and cover the opportunity unless stated.

### Scenario A: detected, resolved, stays resolved

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1 (Oct 28) | OPP-012 present in SF only, 30h old. Decision: `missing_record`, flag, `workflow_gap`. | Episode 1 opens. `first_detected_at = R1`. Label `open`. |
| R2 (Oct 29) | Same. Decision again. | Same episode. `occurrence_count = 2`. |
| Oct 29 18:00 | Resolution event: cause "Flow trigger did not fire for API approvals", fix applied, team Deal Desk Automation. | `operator_claimed_resolved_at = Oct 29 18:00`. Label `claimed_unverified`. |
| R3 (Oct 30) | MC record now exists. No decision for the key. | `observation_absent_since = R3`, `absent_run_count = 1`. Episode closes (`closed_at = R3`). Label `resolved_observed`. |
| R4, R5 (Oct 31, Nov 1) | No decision. | `absent_run_count = 3` → `prevention_validated_at = R5`. Label `prevention_proven`. |

What the design must get right: a claim alone does not close the episode; absence does. Prevention is proven by consecutive clean runs after a claim, not by the claim.

### Scenario B1: resolved on paper, never actually went away

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1, R2 | As Scenario A. | Episode 1 open. |
| Oct 29 18:00 | Resolution event. | `claimed_unverified`. |
| R3 (Oct 30) | Still missing. Decision again. | Same episode (it never closed). Detection after claim → label `claim_contradicted`. The claim is retained, not deleted. |

This is the dashboard's current "Still detected after recorded resolution" banner, now a first-class state. It is one episode, not two. The engine does not say the fix failed; it says the claim is contradicted by evidence and someone should look.

### Scenario B2: went away, then came back

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1 to R5 | As Scenario A. | Episode 1 closed, `prevention_proven`. |
| R9 (Nov 10) | OPP-012 missing from MC again. Decision. | Episode 2 opens on the same key. `first_detected_at = R9`. Episode 1 is not rewritten; it gains `superseded_by_episode = 2`. Key-level metric: recurrence after validated prevention. |

What the design must get right: episode 1's history stands. It was validated at the time, with the evidence available then. The recurrence is a new fact, recorded as a new episode, and the pair (validated, then recurred) is the strongest signal the weekly rollup can surface.

### Scenario C: held, then actionable, one identity

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1 | Contract value differs, SF edited 20 minutes ago. Decision: `below_threshold`, hold. | Episode 1 opens. Occurrence bucket `below_threshold`. |
| R2 | Same, now 25h old. Decision: `shared_field`, recency, auto_correct. | Same episode (key normalizes hold into `field_discrepancy`). `occurrence_count = 2`. |

Preserves the existing `test_missing_record_keeps_incident_identity_after_hold` behavior in the persisted model.

### Scenario D: the rules changed mid-episode

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1, R2 under ruleset `v1` | `contract_value` is shared. Decisions: `shared_field`, recency. | Episode 1. |
| SOP change | `contract_value` becomes Salesforce-owned. Ruleset `v2`. | No projection change. |
| R3 under `v2` | Same disagreement. Decision: `owned_field`, ownership. | Same key, same episode. The bucket changed; `ruleset_version` on each decision explains why. |

What the design must get right: a reader comparing R2 and R3 can see that the data did not change, the policy did. Without `ruleset_version` on the decision this is indistinguishable from a classifier bug.

### Scenario E: absence is only evidence when the run actually looked

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1, R2 | Decisions for OPP-012. | Episode 1 open. |
| R3 | Run `failed` (crashed after observations, before decisions). | No change. A failed run contributes nothing. |
| R4 | Run completed, but OPP-012 was not in either export (no observation for it). | No change. Not covered is not the same as absent. |
| R5 | Run completed, OPP-012 covered, no decision. | `absent_run_count = 1`, episode closes. |

What the design must get right: "no decision" means "resolved" only when the run completed and an observation for that opportunity exists in that run. Otherwise it means "unknown."

## Decision

### Identity

The persisted `incident_key` is a string with three segments joined by `|`:

```
{opportunity_id}|{normalized_bucket}|{subject}
```

- `normalized_bucket` is exactly what `rules.incident_key` produces today: `missing_record`, `field_discrepancy`, or `contract_gap`.
- `subject` is the concept name for field discrepancies and contract gaps. For missing records it is `missing_from:<system>` so direction is part of identity.

`rules.incident_key` gains the direction for missing records. The classifier gains a structured `missing_from` field on `missing_record` and held-missing entries (today it lives only in the `reason` text). This is a pure-function change in `rules.py`. It changes the pinned regression snapshot by adding one field per missing-record entry, which is a deliberate, named behavioral change under the snapshot test's own policy.

`episode_number` starts at 1 per key and increments each time a key reopens after a closed episode.

### Episode boundaries

Runs are processed in `input_cutoff` order. Only `completed` runs participate. For each key:

- **Open.** No open episode exists and a decision for the key appears in run R. Episode `n+1` opens with `first_detected_at = R.input_cutoff`.
- **Continue.** An open episode exists and a decision appears. Append an occurrence. `last_detected_at = R.input_cutoff`. `absent_run_count = 0`. If `operator_claimed_resolved_at` is set and `resolved_at < R.input_cutoff`, the claim is contradicted.
- **Absent.** An open episode exists, run R completed, R covered the opportunity (an `observations` row exists with `reconciliation_run_id = R.id` and that `opportunity_id`), and no decision for the key exists in R. `absent_run_count += 1`. On the first such run, `observation_absent_since = R.input_cutoff`. When `absent_run_count >= episode_close_after_absent_runs`, the episode closes with `closed_at = R.input_cutoff`.
- **Unknown.** Run R did not cover the opportunity, or R is not `completed`. No change.

`episode_close_after_absent_runs` defaults to 1. Rationale: the persistence thresholds already suppress sync lag before a decision is emitted, so one completed run that looked at the opportunity and found agreement is a meaningful signal. Flapping shows up as episodes 1, 2, 3 and feeds the recurrence metric, which is the honest picture. The parameter lives in `contract.yaml` under a new `incident_lifecycle` section and is overridable.

### Closure states

Four independent, nullable timestamps on the episode. They are not a state machine; more than one can be set.

| Column | Set when | Source of truth |
| --- | --- | --- |
| `operator_claimed_resolved_at` | A `resolution_events` row of kind `operator_resolution` attaches to the episode | Human assertion |
| `observation_absent_since` | First completed, covering run with no decision for the key | Engine evidence |
| `independently_verified_at` | A `resolution_events` row of kind `independent_verification` attaches, with `actor` different from the claim's actor | Human assertion by a second party |
| `prevention_validated_at` | Episode is closed, a claim exists, and `absent_run_count >= prevention_validation_runs` | Engine evidence over time |

`prevention_validation_runs` defaults to 3 consecutive clean covering runs. It is an informed guess, parameterized in `contract.yaml`, to be fitted to observed cadence once real runs exist.

Prevention is only "validated" when there was a claim to validate. An episode that closes with no claim and stays closed is `resolved_unattributed`, not `prevention_proven`. The difference matters: one says a named fix held, the other says the problem went away and nobody knows why.

### Derived label

`effective_label` is stored on the episode for query convenience and is a pure function of the columns above. A test rebuilds it and asserts equality. Precedence, first match wins:

| Order | Condition | Label |
| --- | --- | --- |
| 1 | open, claim set, a detection exists with `decided_at > resolved_at` | `claim_contradicted` |
| 2 | open, claim set | `claimed_unverified` |
| 3 | open | `open` |
| 4 | closed, `prevention_validated_at` set | `prevention_proven` |
| 5 | closed, `independently_verified_at` set | `verified` |
| 6 | closed, claim set | `resolved_observed` |
| 7 | closed | `resolved_unattributed` |

Recurrence is a key-level fact, not a relabeling of history. When episode `n+1` opens, episode `n` gains `superseded_by_episode = n+1` and keeps its label. The weekly rollup reports "recurred after validated prevention" by joining the two.

An independent verification that arrives while the episode is still open is recorded but does not change the label; evidence of continued detection outranks a verification claim until the episode closes.

### Resolution attachment and chronology

A resolution event targets an `incident_key`, not an episode. The projection attaches it:

- To the episode of that key that was open at `resolved_at` (`first_detected_at <= resolved_at` and either still open or `resolved_at <= closed_at`).
- Otherwise to the most recent episode with `first_detected_at <= resolved_at`, with `chronology_valid = false` on the attachment.
- If `resolved_at` precedes the key's first detection anywhere, the event is stored, attaches to nothing, and is reported as invalid. This is the existing `latest_prior_resolution` rule made durable.

Resolution events target flagged incidents (`missing_record`, `contract_gap`). Auto-corrected field discrepancies close by observation only. This matches `resolve.py` today and is enforced at the CLI/API layer, not by the database.

### Schema

All timestamps are `timestamptz`. All tables carry `tenant_id` and every cross-table reference is a composite foreign key on `(tenant_id, id)` as established in Increment 1.

**`classification_decisions`** (insert-only)

| Column | Notes |
| --- | --- |
| `id` | UUID PK |
| `tenant_id` | |
| `reconciliation_run_id` | FK `(tenant_id, id)` → `reconciliation_runs`. NOT NULL. |
| `opportunity_id` | |
| `incident_key` | The string defined above. Stored, not recomputed, so a later change to the key function cannot silently re-partition history. |
| `concept` | Nullable. `NULL` for missing records. |
| `missing_from` | Nullable. `salesforce` or `mission_control` for missing records. |
| `bucket` | `owned_field`, `shared_field`, `contract_gap`, `missing_record`, `below_threshold`. CHECK constraint. |
| `action` | `auto_correct`, `flag`, `hold`. CHECK constraint. |
| `rule_applied` | `ownership`, `recency`, `none`. |
| `root_cause` | The category from `rules.root_cause_for`. |
| `sf_observation_id`, `mc_observation_id` | Nullable FKs `(tenant_id, id)` → `observations`. At least one NOT NULL (CHECK). |
| `values` | JSONB: `sf_value`, `mc_value`, `winning_system`, `sf_record_id`. |
| `reason` | Text. |
| `ruleset_version` | NOT NULL. Same value as the run's `ruleset_version`; denormalized so a decision is self-describing. |
| `decided_at` | NOT NULL. Equals the run's `input_cutoff`. |
| `recorded_at` | NOT NULL. Insert time. |
| Unique | `(tenant_id, reconciliation_run_id, incident_key)`. One decision per key per run. |
| Index | `(tenant_id, incident_key, decided_at)`. |

**`resolution_events`** (insert-only)

| Column | Notes |
| --- | --- |
| `id` | UUID PK |
| `tenant_id` | |
| `incident_key` | NOT NULL. |
| `kind` | `operator_resolution` or `independent_verification`. CHECK. |
| `verifies_resolution_id` | Nullable FK to another `resolution_events` row. Required when `kind = independent_verification`. |
| `resolved_at` | NOT NULL. Business time asserted by the human. |
| `recorded_at` | NOT NULL. Insert time. |
| `actor` | NOT NULL. Email or identity string. |
| `responsible_team` | |
| `root_cause_confirmed` | Text. |
| `corrective_action` | Text. |
| `notes` | Text. |
| `source_event_id` | Nullable FK → `source_events`, for resolutions that arrive via event intake later. |
| Index | `(tenant_id, incident_key, resolved_at)`. |

**`incidents`** (rebuildable projection, one row per episode)

| Column | Notes |
| --- | --- |
| `id` | UUID PK |
| `tenant_id`, `incident_key`, `episode_number` | Unique together. |
| `opportunity_id`, `normalized_bucket`, `subject` | Denormalized from the key for querying. |
| `first_detected_at`, `last_detected_at` | From occurrences. |
| `first_run_id`, `last_run_id` | FKs to runs. |
| `occurrence_count` | |
| `last_bucket`, `last_action`, `last_root_cause`, `last_ruleset_version` | From the latest decision. |
| `operator_claimed_resolved_at` | Nullable. |
| `claim_resolution_id` | Nullable FK → `resolution_events`. |
| `observation_absent_since` | Nullable. |
| `absent_run_count` | Default 0. |
| `closed_at` | Nullable. |
| `independently_verified_at` | Nullable. |
| `prevention_validated_at` | Nullable. |
| `superseded_by_episode` | Nullable int. |
| `effective_label` | Derived. CHECK against the seven labels. |
| `rebuilt_through_run_id` | FK to the last run the projection reflects. Makes staleness visible. |
| `rebuilt_at` | |

The `status` column sketched in ADR 0004 is superseded by the explicit timestamps plus `effective_label`.

**`incident_occurrences`** (insert-only within a rebuild)

| Column | Notes |
| --- | --- |
| `incident_id` | FK `(tenant_id, id)` → `incidents`. |
| `classification_decision_id` | FK `(tenant_id, id)` → `classification_decisions`. Unique per tenant: a decision belongs to exactly one episode. |
| `reconciliation_run_id` | Denormalized for run-scoped queries. |

### Projection rebuild

The projection is a pure function of `(completed runs, observations, classification_decisions, resolution_events, contract.incident_lifecycle)`. The rebuild for a tenant deletes that tenant's `incidents` and `incident_occurrences` rows and recomputes them in one transaction. It is idempotent: running it twice yields byte-identical rows (a test asserts this). Full rebuild per tenant is the Increment 2 implementation; incremental rebuild from a watermark is a later optimization and does not change the contract.

Rebuild reads only `completed` runs ordered by `input_cutoff`, then `started_at` as a tie-breaker. A `started` or `failed` run is invisible to the projection.

### Transaction boundaries

- A reconciliation run writes its `reconciliation_runs` row with `status = started`, then observations, then decisions, then flips the run to `completed`, all in one transaction. A crash leaves the run `started` with no `completed_at`; the projection ignores it; restart recovery can see it.
- A resolution event is a single-row insert in its own transaction. It never touches the projection directly.
- A projection rebuild is its own transaction. It reads committed evidence only.

### Configuration

`contract.yaml` gains:

```yaml
incident_lifecycle:
  episode_close_after_absent_runs: 1
  prevention_validation_runs: 3
```

`config.validate_contract` gains checks that both are positive integers. Both are informed guesses, documented as such, like the persistence thresholds.

## Invariants (each one is a test)

1. Every decision's `decided_at` equals its run's `input_cutoff`. A decision never post-dates its run.
2. One decision per `(run, incident_key)`. Enforced by the database.
3. Every decision carries `ruleset_version`. A bucket change on the same key across runs is explainable by comparing versions.
4. Absence is evidence only from a completed run that covered the opportunity. A failed run or an uncovered opportunity changes nothing.
5. A claim never closes an episode. Only observed absence does.
6. Prevention is validated only when a claim exists and `prevention_validation_runs` consecutive clean covering runs follow it.
7. A detection after a claim marks the claim contradicted; the claim is retained.
8. A reopen after close is a new episode. The prior episode's columns and label are never rewritten; it gains only `superseded_by_episode`.
9. Missing-record identity includes direction. Missing-from-MC and missing-from-SF on the same opportunity are different keys.
10. Held and actionable sightings of the same discrepancy share one key and one episode.
11. A resolution dated before the key's first detection attaches to nothing and is reported invalid.
12. Rebuild is deterministic: same evidence, same configuration, byte-identical projection.
13. `effective_label` recomputed from the episode's columns equals the stored value.
14. A decision belongs to exactly one episode.

## Alternatives considered

### Alternative 1: mutable status column updated on write, no rebuild

Rejected. A bug in the transition logic corrupts history with no way back. A policy change (say, `episode_close_after_absent_runs` from 1 to 2) cannot be applied retroactively. The rebuildable projection makes both recoverable.

### Alternative 2: compute lifecycle at query time with SQL views, no `incidents` table

Viable at today's scale and worth naming as the fallback. Rejected for Increment 2 because episode segmentation over weeks across hundreds of keys is a window-function query on every dashboard load, and attaching resolution events to episodes is awkward without a materialized episode row. If the projection ever becomes a maintenance burden, this is the retreat path.

### Alternative 3: one `status` enum instead of four timestamps

Rejected. The reviewer's four states are independent facts with different sources of truth (human claim, engine absence, second-party verification, engine time-series). Collapsing them into one enum forces an ordering that does not exist and loses information when more than one is true.

### Alternative 4: separate `verifications` table

Rejected as unnecessary. A `kind` column plus `verifies_resolution_id` on `resolution_events` keeps one append-only stream of human assertions and lets the independence rule (`actor` differs) be a simple application check.

### Alternative 5: close an episode on the claim

Rejected explicitly, because it is the mistake the whole operating model exists to avoid. Scenario B1 is the counterexample.

## Consequences

### Positive

- The four closure states are distinct, queryable, and each has a named source of truth.
- Recurrence after validated prevention becomes a direct join, not a heuristic. It is the single most useful number the weekly rollup can report.
- History survives projection rebuilds, rule changes, and application restarts. The evidence tables are the record; the projection is a view that can be regenerated.
- The identity bug on missing-record direction is fixed at the source and pinned by a test.
- Every Phase 3 and Phase 4 metric (time to resolution, recurrence rate, SLA compliance, prevention verification) has a defined column to read from.

### Negative

- The regression snapshot changes once, deliberately, when `missing_from` is added to the classifier output. The commit says so.
- `resolve.py` and the JSON audit log remain the demo path until the batch loader is wired to Postgres (Increment 3). During Increment 2 the CLI dual-writes: it still annotates the JSON log and also inserts a `resolution_events` row when `DATABASE_URL` is set. Two write paths for one release is a known cost, documented, and retired in Increment 3.
- Full rebuild per tenant is O(all decisions). Fine for this project's scale; the watermark optimization is noted, not built.

## Increment plan

**Increment 2a** (small, bounded): `missing_from` on classifier output and in `rules.incident_key`; snapshot regeneration in the same commit; migration `0002_classification_decisions`; `db/writes.insert_classification_decision`; `incident_lifecycle` section in `contract.yaml` with validation; tests for invariants 1, 2, 3, 9, 10. No projection yet.

**Increment 2b**: migration `0003_resolution_events_and_incidents`; `db/projection.rebuild_incidents(tenant_id)`; `resolve.py` dual-write; `tests/test_incident_lifecycle.py` with Scenarios A through E as fixtures; tests for invariants 4 through 8 and 11 through 14.

**Not in Increment 2**: wiring `reconcile.py` or the API to write to Postgres, the dashboard reading from Postgres, any write-back, any worker. Those are Increment 3, and they depend on this contract being stable.

## Open questions for review

1. `episode_close_after_absent_runs = 1` versus 2. One is my default and the reasoning is above. If the reviewer's operational experience says sync systems produce single-run false agreement often enough to matter, 2 is the right default and the parameter already supports it.
2. Should `prevention_validation_runs` be run-count based (current) or wall-clock based (for example, 14 days)? Run-count is deterministic for tests and independent of schedule; wall-clock matches how a RevOps lead would describe it. Proposal: run-count now, add an optional `prevention_validation_days` later if real cadence makes run-count misleading.
3. Independence rule for verification is `actor != claim actor`. Is that sufficient, or should verification require a different `responsible_team`?

## References

- `rules.py`: `incident_key`, `latest_prior_resolution`, classifier output shape.
- `resolve.py`: current resolution capture and chronology enforcement.
- `docs/decisions/0004-postgres-event-store-docker-compose.md`: the three-layer model and the open item this ADR closes.
- `db/models.py`, `db/writes.py`: Increment 1 tables and the write-boundary validation pattern this ADR extends.
