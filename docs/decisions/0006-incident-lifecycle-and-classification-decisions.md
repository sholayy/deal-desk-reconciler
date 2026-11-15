# ADR 0006: Classification decisions and incident lifecycle

**Status:** Proposed (revision 3)
**Date:** 2026-11-14, revised 2026-11-15 (r2 and r3)
**Decision owner:** Deal Desk Engineering
**Closes:** the "Open" item in ADR 0004 (episode-closing policy and the evidence threshold for a verified fix)

**Revision 2 changes**, in response to engineering review: evaluation outcomes become first-class evidence and only a positive match counts toward closure; closure and post-closure monitoring are separated; `prevention_proven` is renamed `correction_sustained` and `prevention_validated` is reserved for when control execution is observable; the run transaction is split in two; projection IDs are deterministic; cross-row invariants name their enforcement mechanism; a sixth scenario covers a null on one side.

**Revision 3 changes**, in response to the second review: a run records what it was expected to evaluate (`run_input_manifest`), so completeness is a positive claim and not the absence of a bad row; completion is gated on the actual evaluation count equalling the expected count; `BOTH_PRESENT` is defined against the manifest so it proves the specific expected records exist and are linked by shared `opportunity_id`, not that two arbitrary records happen to be present; `incident_key` serialization is specified as canonical before it is hashed to a UUIDv5, so logically identical keys cannot drift to different projection IDs.

## Context

ADR 0004 established the three-layer model: immutable evidence, a rebuildable incident projection, and operational analytics on top. Increment 1 landed the evidence tables for runs, source events and observations. It left one design question open: when does an incident episode close, and what counts as proof that a fix held?

The reviewer named four states the projection must keep apart:

1. Someone marked the issue resolved.
2. The issue stopped appearing in reconciliation.
3. Someone independently verified the correction.
4. A preventive control held over subsequent relevant activity.

Four facts about the current code shape this ADR:

- `rules.incident_key(entry)` defines identity as `(opportunity_id, concept, normalized_bucket)`, where `normalized_bucket` collapses `owned_field` and `shared_field` into `field_discrepancy` and folds `below_threshold` (hold) into the identity of the actionable discrepancy it will become. Tests pin this. The persisted key must be the same identity.
- The missing-record entry carries direction only inside its `reason` string. `concept` is `None`. Missing-from-MC and missing-from-SF on the same opportunity hash to the same key today. ADR 0004 required direction in the key; this ADR adds it.
- The classifier skips a field comparison when either side is null (`test_null_value_is_skipped_silently` pins this). The absence of a discrepancy row therefore does not mean the two systems agreed. It may mean nothing was compared. Revision 1 of this ADR treated "no decision" as evidence of correction. That was wrong, and it is the most consequential correction in this revision.
- `resolve.py` attaches a resolution only to `action == "flag"` entries and enforces `original_observation < resolved_at`. `rules.latest_prior_resolution` enforces `original <= resolved < repeat`. Those chronology rules carry forward unchanged.

This ADR is written scenario-first. The schema is derived from what the scenarios need.

## Evaluation outcomes: the evidence unit

Before the scenarios, the unit of evidence has to be defined, because every closure rule depends on it.

A reconciliation run evaluates each concept for each covered opportunity and records one outcome:

| Outcome | Meaning | Produces a decision? | Counts toward closure? |
| --- | --- | --- | --- |
| `MATCH` | Both inputs present, comparable, and equal under the SOP rule | No | Yes |
| `MISMATCH` | Both inputs present, comparable, and different | Yes (one `classification_decision`) | No |
| `INSUFFICIENT_EVIDENCE` | Opportunity covered, but a required input was null or unparseable | No | No |
| `NOT_EVALUATED` | Concept excluded by policy for this opportunity (`salesforce_only`, `mission_control_only`), or opportunity not covered by the run | No | No |

For the record-presence concept (the basis of missing-record incidents) the outcomes are:

| Outcome | Meaning | Counts toward closure of a `missing_record` episode? |
| --- | --- | --- |
| `BOTH_PRESENT` | The run's `run_input_manifest` for this `opportunity_id` has `present_in_salesforce = true` and `present_in_mission_control = true`, which means a Salesforce record and a Mission Control record keyed to the same `opportunity_id` were both observed at this run's `input_cutoff`. Shared-key linkage is the proof; two unlinked records do not qualify. | Yes |
| `ONE_SIDE` | The manifest has exactly one side true. Direction is recorded in the evaluation `detail`. | No (produces `missing_record` or a held decision) |

A held decision (`below_threshold`) is a `MISMATCH` or `ONE_SIDE` whose age is under the persistence threshold. It is still a sighting. It never counts toward closure.

The classifier gains a pure companion function, `rules.evaluate_concepts(joined, contract, mapping, run_timestamp)`, that returns the full outcome matrix. `classify_discrepancies` is unchanged and remains derivable from the matrix (it is the `MISMATCH` and `ONE_SIDE` subset). The regression snapshot for `classify_discrepancies` is therefore unaffected by this function; it changes only when `missing_from` is added (see Identity).

## Scenarios the design must answer

Each scenario becomes a lifecycle fixture in `tests/test_incident_lifecycle.py`. Runs are identified by their business-time `input_cutoff`. All runs are `completed` and cover the opportunity unless stated. Thresholds use the defaults: close after 1 qualified match, sustain after 3.

### Scenario A: detected, resolved, stays resolved

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1 (Oct 28) | OPP-012 `ONE_SIDE` (SF only), 30h old. Decision: `missing_record`, flag, `workflow_gap`. | Episode 1 opens. `first_detected_at = R1`. Label `open`. |
| R2 (Oct 29) | `ONE_SIDE`. Decision again. | Same episode. `occurrence_count = 2`. |
| Oct 29 18:00 | Resolution event: cause "Flow trigger did not fire for API approvals", fix applied. | `operator_claimed_resolved_at` set. Label `claimed_unverified`. |
| R3 (Oct 30) | `BOTH_PRESENT`. | `first_clean_at = R3`, `clean_evaluation_count = 1`. Episode closes, `closed_at = R3`. Label `resolved_observed`. Post-closure monitoring begins. |
| R4 (Oct 31) | `BOTH_PRESENT`. | `clean_evaluation_count = 2`. Still monitoring. |
| R5 (Nov 1) | `BOTH_PRESENT`. | `clean_evaluation_count = 3` → `correction_sustained_at = R5`. Label `correction_sustained`. Monitoring ends. |

A claim does not close the episode; a qualified clean evaluation does. Sustained correction needs consecutive clean evaluations after closure, not just closure.

### Scenario B1: resolved on paper, never went away

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1, R2 | As A. | Episode 1 open. |
| Oct 29 18:00 | Resolution event. | `claimed_unverified`. |
| R3 (Oct 30) | `ONE_SIDE`. Decision again. | Same episode (never closed). Detection after claim → `claim_contradicted`. The claim is retained. |

One episode, not two. The engine does not say the fix failed; it says the claim is contradicted by evidence.

### Scenario B2: went away, then came back

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1 to R5 | As A. | Episode 1 closed, `correction_sustained`. |
| R9 (Nov 10) | `ONE_SIDE` again. Decision. | Episode 2 opens on the same key. Episode 1 is unchanged except `superseded_by_episode = 2`. Key-level metric: recurrence after sustained correction. |

### Scenario B3: came back before correction was sustained

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1 to R3 | As A. | Episode 1 closed at R3, monitoring, `clean_evaluation_count = 1`. |
| R4 | `ONE_SIDE`. Decision. | Episode 1 monitoring ends, `correction_sustained_at` stays null, `superseded_by_episode = 2`. Episode 2 opens at R4. Label on episode 1 stays `resolved_observed`. |

Reappearance during monitoring is a new episode, and the prior episode never earns `correction_sustained`.

### Scenario C: held, then actionable, one identity

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1 | `contract_value` `MISMATCH`, SF edited 20 minutes ago. Decision: `below_threshold`, hold. | Episode 1 opens. Occurrence bucket `below_threshold`. |
| R2 | `MISMATCH`, 25h old. Decision: `shared_field`, recency, auto_correct. | Same episode. `occurrence_count = 2`. |

### Scenario D: the rules changed mid-episode

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1, R2 under ruleset `v1` | `contract_value` shared. Decisions: `shared_field`, recency. | Episode 1. |
| SOP change | `contract_value` becomes Salesforce-owned. Ruleset `v2`. | No change. |
| R3 under `v2` | Same disagreement. Decision: `owned_field`, ownership. | Same key, same episode. `ruleset_version` on each decision explains the bucket change. |

### Scenario E: absence is only evidence when the run actually looked

| Run | Evidence | Projection after run |
| --- | --- | --- |
| R1, R2 | Decisions for OPP-012. | Episode 1 open. |
| R3 | Run `failed`. | No change. |
| R4 | Run completed. OPP-012 not in `run_input_manifest` (in neither export at R4 `input_cutoff`). No `concept_evaluations` rows for OPP-012. | No change. Not in scope is not clean. |
| R5 | OPP-012 in the manifest with both sides true. `record_presence` outcome `BOTH_PRESENT`. | `clean_evaluation_count = 1`, episode closes. |

### Scenario F: a null on one side is not agreement

| Run | SF owner | MC contact | Outcome | Projection after run |
| --- | --- | --- | --- | --- |
| R1 | Alice | Bob | `MISMATCH` | Episode 1 opens (`owned_field`, ownership). |
| R2 | Alice | NULL | `INSUFFICIENT_EVIDENCE` | No change. Episode stays open. Nothing was compared. |
| R3 | Alice | NULL | `INSUFFICIENT_EVIDENCE` | No change. |
| R4 | Alice | Alice | `MATCH` | `clean_evaluation_count = 1`, episode closes. |

This is the scenario revision 1 got wrong. Under revision 1, R2 closed the episode. Under revision 2, the episode stays open until a run positively establishes agreement. `INSUFFICIENT_EVIDENCE` is surfaced in the rollup as its own count, which also retires the README's "silently skipped" limitation.

## Decision

### Identity

The persisted `incident_key` is a string with three segments joined by `|`:

```
{opportunity_id}|{normalized_bucket}|{subject}
```

- `normalized_bucket` is what `rules.incident_key` produces today: `missing_record`, `field_discrepancy`, or `contract_gap`.
- `subject` is the concept name for field discrepancies and contract gaps. For missing records it is `missing_from:<system>`.

`rules.incident_key` gains the direction for missing records. The classifier gains a structured `missing_from` field on `missing_record` and held-missing entries. This is a pure-function change in `rules.py` that changes the pinned regression snapshot by adding one field per missing-record entry. The commit that lands it says so.

The serialized form is canonical, because any variation in the string changes the UUIDv5 of the projection row it belongs to and would split one incident across two episodes:

- Segment separator is `|`. Segments contain no `|`; the classifier rejects an input that would introduce one.
- All segment values are Unicode NFC-normalized and lowercased. Salesforce IDs preserve their case by convention but are lowercased in the key so that any casing drift upstream cannot produce a distinct identity.
- `missing_from` values are the literals `salesforce` and `mission_control`. No other casing or spelling is accepted.
- `concept` and `subject` are drawn from `contract.yaml` names, which are already a closed set validated at load time.

`rules.incident_key` is the only place in the codebase that produces this string. A test enumerates the inputs across casing, whitespace and NFC-vs-NFD and pins that logically identical inputs produce one string.

`episode_number` starts at 1 per key and increments each time a key reopens after a closed episode.

### Episode lifecycle

Runs are processed in `input_cutoff` order, `started_at` as tie-breaker. Only `completed` runs participate. For each key, the relevant evaluation for a run is: the record-presence outcome for `missing_record` keys; the concept outcome for `field_discrepancy` and `contract_gap` keys.

**Open episode.** On a decision for the key: if no open episode exists, open episode `n+1` with `first_detected_at = R.input_cutoff`. Otherwise append an occurrence, set `last_detected_at`, reset `clean_evaluation_count` to 0. If a claim exists with `resolved_at < R.input_cutoff`, the claim is contradicted.

**Clean evaluation while open.** On a qualifying clean outcome (`MATCH` or `BOTH_PRESENT`): `clean_evaluation_count += 1`. On the first, set `first_clean_at`. When `clean_evaluation_count >= episode_close_after_clean_evaluations`, set `closed_at = R.input_cutoff` and enter monitoring.

**Monitoring after close.** The closed episode keeps counting qualifying clean outcomes. When `clean_evaluation_count >= correction_sustain_evaluations` and a claim exists, set `correction_sustained_at = R.input_cutoff` and stop monitoring. If no claim exists, monitoring stops at the same threshold without setting `correction_sustained_at`; the episode is `resolved_unattributed`.

**Reappearance.** A decision for the key while the previous episode is closed (monitoring or not) opens a new episode and sets `superseded_by_episode` on the previous one. Monitoring on the previous episode stops. Its columns are otherwise untouched by that event.

**No signal.** `INSUFFICIENT_EVIDENCE`, `NOT_EVALUATED`, a `failed` or `started` run, or a run that did not cover the opportunity: no change to any counter, open or monitoring.

Defaults: `episode_close_after_clean_evaluations = 1`, `correction_sustain_evaluations = 3`. The reviewer's qualification on the first is adopted and is the point of the evaluation-outcome model: one properly evaluated match is stronger evidence than ten runs that never evaluated the concept. Both parameters live in `contract.yaml` under `incident_lifecycle` and are documented as informed guesses to be fitted to observed cadence.

### Closure states

Four independent, nullable timestamps on the episode. They are not a state machine.

| Column | Set when | Source of truth |
| --- | --- | --- |
| `operator_claimed_resolved_at` | A `resolution_events` row of kind `operator_resolution` attaches | Human assertion |
| `first_clean_at` | First qualifying clean evaluation for the key | Engine evidence |
| `independently_verified_at` | A `resolution_events` row of kind `independent_verification` attaches, with `actor` different from the claim's actor | Second-party human assertion |
| `correction_sustained_at` | Closed, claim exists, and `clean_evaluation_count >= correction_sustain_evaluations` | Engine evidence over time |

`prevention_validated_at` is deliberately not in this increment. Sustained absence of a discrepancy is not proof that a preventive control was exercised; the control may simply not have been triggered by any new transaction. That state requires observing control execution, which arrives with orchestration (Increment 3 onward). Until then the strongest claim the system makes is `correction_sustained`.

### Derived label

`effective_label` is stored on the episode and is a pure function of its columns. A test recomputes and asserts equality. Precedence, first match wins:

| Order | Condition | Label |
| --- | --- | --- |
| 1 | open, claim set, a detection exists with `decided_at > resolved_at` | `claim_contradicted` |
| 2 | open, claim set | `claimed_unverified` |
| 3 | open | `open` |
| 4 | closed, `correction_sustained_at` set | `correction_sustained` |
| 5 | closed, `independently_verified_at` set | `verified` |
| 6 | closed, claim set | `resolved_observed` |
| 7 | closed | `resolved_unattributed` |

Recurrence is a key-level fact read by joining episodes `n` and `n+1`; it is not a label rewrite. An independent verification that arrives while the episode is open is recorded but does not change the label.

### Resolution attachment and chronology

A resolution event targets an `incident_key`. The projection attaches it to the episode open at `resolved_at` (`first_detected_at <= resolved_at` and either open or `resolved_at <= closed_at`). Otherwise it attaches to the most recent episode with `first_detected_at <= resolved_at` with `chronology_valid = false`. If `resolved_at` precedes the key's first detection anywhere, the event attaches to nothing and is reported invalid. This is `latest_prior_resolution` made durable.

Resolution events target flagged incidents (`missing_record`, `contract_gap`). Auto-corrected field discrepancies close by evaluation only. Enforced at the CLI and API layer.

### Schema

All timestamps are `timestamptz`. All tables carry `tenant_id`. Cross-table references are composite foreign keys that include `tenant_id`, as established in Increment 1.

**`reconciliation_runs`** (existing; two additions)

Add `UNIQUE (tenant_id, id, input_cutoff, ruleset_version)`. This is not for lookups. It exists so a foreign key from decisions can include `decided_at` and `ruleset_version`, which makes the database enforce that a decision's business time and ruleset equal its run's. Migration `0002` adds it.

Add `expected_evaluation_count` (integer, NOT NULL, CHECK >= 0). Written in Transaction A at run start from the manifest Transaction A commits. Transaction B's `UPDATE runs SET status = 'completed'` carries the guard `WHERE (SELECT COUNT(*) FROM concept_evaluations WHERE reconciliation_run_id = runs.id) = runs.expected_evaluation_count`, so a run cannot be marked `completed` if any expected evaluation is missing. The guard runs inside Transaction B; a mismatch makes the `UPDATE` affect zero rows and the write helper raises, which rolls back Transaction B and leaves the run in `started` for recovery.

**`run_input_manifest`** (insert-only; written in Transaction A)

| Column | Notes |
| --- | --- |
| `tenant_id` | |
| `reconciliation_run_id` | FK → runs |
| `opportunity_id` | Not null. One row per in-scope opportunity. |
| `present_in_salesforce` | Boolean, NOT NULL. True if a Salesforce record with this `opportunity_id` exists at the run's `input_cutoff`. |
| `present_in_mission_control` | Boolean, NOT NULL. Same test, Mission Control side. |
| CHECK | `present_in_salesforce OR present_in_mission_control`. An opportunity with neither side present has no reason to be in the manifest. |
| PK | `(tenant_id, reconciliation_run_id, opportunity_id)` |

The manifest fixes the scope of the run before any concept is evaluated. It answers two questions the projection and the reviewer need answered: was this opportunity in scope for this run, and for the `record_presence` concept, which sides were observed to exist. A retry of a crashed Transaction B uses the same manifest, so coverage does not drift if the underlying sources change between Transaction A and the retry. Volume at 500 opportunities is 500 rows per run. Retention tracks the run that owns the row.

**`concept_evaluations`** (insert-only)

| Column | Notes |
| --- | --- |
| `id` | UUID PK |
| `tenant_id` | |
| `reconciliation_run_id` | Composite FK → runs |
| `opportunity_id` | |
| Manifest FK | `(tenant_id, reconciliation_run_id, opportunity_id)` → `run_input_manifest`. Makes an evaluation for an out-of-scope opportunity impossible: the FK fails. |
| `concept` | Concept name, or `record_presence` |
| `outcome` | `MATCH`, `MISMATCH`, `INSUFFICIENT_EVIDENCE`, `NOT_EVALUATED`, `BOTH_PRESENT`, `ONE_SIDE`. CHECK. |
| `sf_observation_id`, `mc_observation_id` | Nullable composite FKs → observations |
| `detail` | JSONB: which input was missing, parse errors, rule applied. For `ONE_SIDE` the detail names which side. |
| `evaluated_at` | Equals run `input_cutoff` (enforced via the same FK pattern as decisions) |
| Unique | `(tenant_id, reconciliation_run_id, opportunity_id, concept)` |
| Index | `(tenant_id, opportunity_id, concept, evaluated_at)` |

Volume: manifest opportunities times evaluation targets per run, where evaluation targets are the 9 reconciled concepts plus the 1 contract gap plus the `record_presence` pseudo-concept, for 11 per opportunity. At 500 in-scope opportunities that is 5,500 rows per run. Trivial for Postgres at this scale. An opportunity not in the manifest has no evaluation row; coverage is read from `run_input_manifest`, which is the authoritative source for what the run was expected to see. Retention policy is an open question below.

`expected_evaluation_count` on the run row equals `COUNT(*) FROM run_input_manifest WHERE reconciliation_run_id = ...` times 11 at Transaction A commit time. The multiplier is a constant for a given ruleset version and is kept in `contract.yaml` as a derived value (not an editable one) so a ruleset change produces a different expected count for runs under that ruleset.

**`classification_decisions`** (insert-only)

| Column | Notes |
| --- | --- |
| `id` | UUID PK |
| `tenant_id` | |
| `reconciliation_run_id` | NOT NULL |
| `decided_at` | NOT NULL |
| `ruleset_version` | NOT NULL |
| FK | `(tenant_id, reconciliation_run_id, decided_at, ruleset_version)` → runs `(tenant_id, id, input_cutoff, ruleset_version)`. The database rejects a decision whose time or ruleset disagrees with its run. |
| `evaluation_id` | Composite FK → `concept_evaluations`. NOT NULL. A decision is always backed by a `MISMATCH` or `ONE_SIDE` evaluation. |
| `opportunity_id` | |
| `incident_key` | Stored, not recomputed. |
| `concept` | Nullable. `NULL` for missing records. |
| `missing_from` | Nullable. `salesforce` or `mission_control`. |
| `bucket` | `owned_field`, `shared_field`, `contract_gap`, `missing_record`, `below_threshold`. CHECK. |
| `action` | `auto_correct`, `flag`, `hold`. CHECK. |
| `rule_applied` | `ownership`, `recency`, `none`. |
| `root_cause` | From `rules.root_cause_for`. |
| `values` | JSONB: `sf_value`, `mc_value`, `winning_system`, `sf_record_id`. |
| `reason` | Text. |
| `recorded_at` | Insert time. |
| Unique | `(tenant_id, reconciliation_run_id, incident_key)` |
| Index | `(tenant_id, incident_key, decided_at)` |

**`resolution_events`** (insert-only)

| Column | Notes |
| --- | --- |
| `id` | UUID PK |
| `tenant_id` | |
| `incident_key` | NOT NULL |
| `kind` | `operator_resolution` or `independent_verification`. CHECK. |
| `verifies_resolution_id` | Nullable self-FK. CHECK: NOT NULL when kind is `independent_verification`, NULL otherwise. |
| `resolved_at` | NOT NULL. Business time. |
| `recorded_at` | NOT NULL |
| `actor` | NOT NULL |
| `responsible_team`, `root_cause_confirmed`, `corrective_action`, `notes` | Text |
| `source_event_id` | Nullable FK → `source_events` |
| Index | `(tenant_id, incident_key, resolved_at)` |

Actor independence (`verification.actor != claim.actor`) is a cross-row rule and is enforced in `db/writes.insert_resolution_event`, with a negative test. Authenticated identity is out of scope for this increment and noted.

**`incidents`** (rebuildable projection, one row per episode)

| Column | Notes |
| --- | --- |
| `id` | UUIDv5 of `(tenant_id, incident_key, episode_number)` under a project namespace. Deterministic across rebuilds. |
| `tenant_id`, `incident_key`, `episode_number` | Unique together |
| `opportunity_id`, `normalized_bucket`, `subject` | Denormalized from the key |
| `first_detected_at`, `last_detected_at`, `first_run_id`, `last_run_id` | From occurrences |
| `occurrence_count` | |
| `last_bucket`, `last_action`, `last_root_cause`, `last_ruleset_version` | From the latest decision |
| `operator_claimed_resolved_at`, `claim_resolution_id` | Nullable |
| `first_clean_at` | Nullable |
| `clean_evaluation_count` | Default 0 |
| `closed_at` | Nullable |
| `monitoring` | Boolean. True between close and sustain or reappearance. |
| `independently_verified_at` | Nullable |
| `correction_sustained_at` | Nullable |
| `superseded_by_episode` | Nullable int |
| `effective_label` | Derived. CHECK against the seven labels. |
| `rebuilt_through_run_id`, `rebuilt_at` | Staleness visibility |

**`incident_occurrences`**

| Column | Notes |
| --- | --- |
| `tenant_id`, `incident_id`, `classification_decision_id` | Composite PK on `(tenant_id, classification_decision_id)`. A decision belongs to exactly one episode. |
| `reconciliation_run_id` | Denormalized |

No surrogate UUID on occurrences; the natural key is the identity.

### Projection rebuild

The projection is a pure function of `(completed runs, observations, concept_evaluations, classification_decisions, resolution_events, contract.incident_lifecycle)`. Rebuild for a tenant deletes that tenant's `incidents` and `incident_occurrences` rows and recomputes them in one transaction. Because incident IDs are UUIDv5 of their natural key, a rebuild from the same evidence and configuration yields identical rows including IDs (a test asserts this).

What is immutable is the evidence. Projection rows are derived and may legitimately change when configuration changes (for example, raising `correction_sustain_evaluations` from 3 to 5 would clear `correction_sustained_at` on episodes that only reached 3). That is correct behavior for a projection, and the rebuild is how a policy change is applied retroactively and consistently.

### Transaction boundaries

1. **Transaction A:** compute the input manifest (query both source systems at `input_cutoff` for opportunities visible in either), then in one commit insert the `reconciliation_runs` row with `status = started` and `expected_evaluation_count` set, and bulk-insert the `run_input_manifest` rows. Transaction A commits only if both sources were queryable; if a source is unreachable, nothing is written and the run never starts. The run now exists durably with its scope frozen.
2. **Transaction B:** insert observations and concept evaluations, insert classification decisions, and update the run to `completed` with `completed_at`. The `UPDATE` carries the guard `WHERE expected_evaluation_count = (SELECT COUNT(*) FROM concept_evaluations WHERE reconciliation_run_id = runs.id)`. If the guard fails, the `UPDATE` affects zero rows, the write helper raises, and the entire Transaction B rolls back. The run stays `started` and the recovery step takes it from there. The guard makes a `completed` run a positive claim that every expected evaluation was recorded.
3. **Recovery:** a run left in `started` past a timeout is marked `failed` by a recovery step. The projection ignores `started` and `failed` runs either way. A retry of Transaction B after a crash uses the already-committed manifest, so scope does not drift between attempts.
4. A resolution event is a single-row insert in its own transaction.
5. A projection rebuild is its own transaction over committed evidence only.

### Configuration

`contract.yaml` gains:

```yaml
incident_lifecycle:
  episode_close_after_clean_evaluations: 1
  correction_sustain_evaluations: 3
```

`config.validate_contract` checks both are positive integers and that the second is not smaller than the first.

## Invariants and how each is enforced

| # | Invariant | Enforcement | Test |
| --- | --- | --- | --- |
| 1 | A decision's `decided_at` equals its run's `input_cutoff` | Composite FK including `input_cutoff` | Insert with mismatched time → `IntegrityError` |
| 2 | A decision's `ruleset_version` equals its run's | Same composite FK | Insert with mismatched version → `IntegrityError` |
| 3 | One decision per `(run, incident_key)` | Unique constraint | Duplicate insert → `IntegrityError` |
| 4 | Every decision is backed by an evaluation whose outcome is `MISMATCH` or `ONE_SIDE` | FK plus write-helper check on outcome | Decision referencing a `MATCH` evaluation → `ValueError` |
| 5 | Only `MATCH` / `BOTH_PRESENT` advance closure or sustain counters | Projection logic | Scenarios E and F |
| 6 | A claim never closes an episode | Projection logic | Scenario B1 |
| 7 | Closure requires `episode_close_after_clean_evaluations` qualified clean outcomes | Projection logic | Scenario A |
| 8 | Sustained correction requires a claim and `correction_sustain_evaluations` clean outcomes, counted across closure | Projection logic | Scenarios A and B3 |
| 9 | A detection after a claim marks the claim contradicted; the claim is retained | Projection logic | Scenario B1 |
| 10 | A reopen is a new episode; the prior episode gains only `superseded_by_episode` from that event | Projection logic | Scenarios B2 and B3 |
| 11 | Missing-record identity includes direction | `rules.incident_key` | Two directions on one opportunity → two keys |
| 12 | Held and actionable sightings share one key | `rules.incident_key` | Existing test, extended to persisted key |
| 13 | A resolution dated before the key's first detection attaches to nothing | Projection logic | Scenario fixture |
| 14 | Independent verifier differs from claimant | Write helper | Same-actor verification → `ValueError` |
| 15 | Rebuild is deterministic including IDs | UUIDv5 identity, ordered processing | Rebuild twice, compare row sets |
| 16 | `effective_label` recomputed equals stored | Projection logic | Recompute and compare |
| 17 | A decision belongs to exactly one episode | Composite PK on occurrences | Second attachment → `IntegrityError` |
| 18 | Naive timestamps rejected at the write boundary | Write helpers (existing pattern) | Existing test, extended to new helpers |
| 19 | Every `concept_evaluations` row targets an in-scope opportunity | Composite FK to `run_input_manifest` | Insert for an opportunity not in the manifest → `IntegrityError` |
| 20 | A run reaches `completed` only when its actual evaluation count equals `expected_evaluation_count` | Guarded `UPDATE` inside Transaction B | Insert one fewer evaluation than expected → write helper raises; run stays `started` |
| 21 | `incident_key` serialization is canonical across casing, whitespace, Unicode form and `missing_from` spelling | `rules.incident_key` normalization | Parametrized test enumerating drift cases returns one string |
| 22 | `BOTH_PRESENT` is only recorded when the manifest row has both sides true for this opportunity | Write helper reads the manifest row; outcome and manifest must agree | Write a `BOTH_PRESENT` evaluation where the manifest has one side false → `ValueError` |

## Alternatives considered

### Alternative 1: infer closure from the absence of a decision row

This was revision 1. Rejected because the classifier skips null comparisons, so absence of a decision proves nothing. Scenario F is the counterexample. The evaluation-outcome table plus the manifest costs 11 + 1 inserts per opportunity per run and is the price of being able to say "the systems agreed on this specific concept for this in-scope opportunity" rather than "nothing complained."

### Alternative 1b: evaluations table alone, no manifest

Rejected after review 2. A unique constraint on `(run, opportunity, concept)` prevents duplicate evaluations but does not prove that every expected evaluation happened. A run that silently skipped five opportunities would record no `MATCH` or `MISMATCH` for them and the projection would see nothing to count against closure. The manifest makes the expected set the DB's business: the FK stops out-of-scope evaluations from being inserted, and the count guard stops a run from being marked `completed` while an expected evaluation is missing.

### Alternative 2: mutable status column updated on write, no rebuild

Rejected. A logic bug corrupts history irrecoverably; a policy change cannot be applied retroactively.

### Alternative 3: compute lifecycle at query time with views, no `incidents` table

Viable at today's scale and named as the fallback. Rejected for Increment 2 because episode segmentation across weeks is a window-function query on every dashboard load, and attaching resolution events to episodes is awkward without a materialized row.

### Alternative 4: one `status` enum instead of independent timestamps

Rejected. The four states have different sources of truth and more than one can be true at once.

### Alternative 5: keep `prevention_validated` with the three-clean-runs rule

Rejected after review. Three clean runs show the discrepancy stayed absent, not that a preventive control was exercised. Naming it "prevention" would make every downstream metric overclaim.

### Alternative 6: random UUIDs on projection rows

Rejected. Rebuilds would churn IDs, breaking any downstream reference and making "deterministic rebuild" untestable. UUIDv5 over the natural key costs nothing and fixes both.

### Alternative 7: one transaction for the whole run

Rejected after review. It cannot both be atomic and leave a durable `started` marker on crash. Two transactions give durable execution history without exposing partial runs as evidence.

## Consequences

### Positive

- "The systems agreed" becomes a recorded fact, distinct from "nothing was compared." Every closure and every time-to-resolution figure rests on positive evidence.
- The README's "silently skipped" limitation is retired: `INSUFFICIENT_EVIDENCE` is counted and surfaced.
- The four closure states are distinct and each has a named source of truth. The system never claims more than its evidence supports.
- Recurrence after sustained correction is a direct join.
- Two cross-row invariants that revision 1 only asserted are now database-enforced through the composite FK.
- Evidence survives rebuilds, rule changes and restarts. Projection rows are stable across rebuilds because their IDs derive from their identity.

### Negative

- `concept_evaluations` is the largest table in the system by row count. Retention is an open question.
- The regression snapshot changes once when `missing_from` lands.
- `resolve.py` dual-writes (JSON log plus `resolution_events`) during Increment 2 until the batch loader is wired to Postgres in Increment 3.
- `prevention_validated` remains undelivered until control execution is observable. The rollup will say `correction_sustained`, which is the honest ceiling for now.

## Increment plan

**Increment 2a: classification evidence.** `missing_from` on classifier output and in `rules.incident_key`, with the snapshot regenerated in the same commit. Canonical-serialization normalization in `rules.incident_key` with the drift test. `rules.evaluate_concepts` as a pure function with its own tests. `rules.compute_manifest` as a pure function over the two input snapshots. Migration `0002`: the runs unique key, `expected_evaluation_count` on runs, `run_input_manifest`, `concept_evaluations` with its FK to the manifest, `classification_decisions`. Write helpers `insert_run_with_manifest` (one call writes both in Transaction A), `insert_concept_evaluation` with the manifest-agreement check, `insert_classification_decision` with the outcome check, and `mark_run_completed` carrying the count guard. `incident_lifecycle` in `contract.yaml` with validation. Tests for invariants 1, 2, 3, 4, 11, 12, 18, 19, 20, 21, 22.

**Increment 2b: lifecycle projection.** Migration `0003`: `resolution_events`, `incidents`, `incident_occurrences`. `db/projection.rebuild_incidents(tenant_id)` implementing the lifecycle above including monitoring. `resolve.py` dual-write. `tests/test_incident_lifecycle.py` with Scenarios A, B1, B2, B3, C, D, E, F as fixtures. Tests for invariants 5 through 10, 13, 15, 16, 17.

**Increment 2c: operational verification.** `insert_resolution_event` with the independence check (invariant 14). Rebuild stability under configuration change. Key-level recurrence and time-to-first-clean metrics as SQL. Chronology edge cases.

**Not in Increment 2:** wiring `reconcile.py` or the API to write to Postgres, the dashboard reading from Postgres, write-back, workers, `prevention_validated`.

## Decided during review

| Question | Decision |
| --- | --- |
| Close after 1 or 2 clean evaluations | 1, qualified: the evaluation must positively establish agreement |
| Run count or elapsed time for sustain | Run count |
| Different actor or different team for verification | Different actor; authenticated identity and team governance later |

## Open questions for review

1. **Retention for `concept_evaluations` and `run_input_manifest`.** Proposal: keep every evaluation row for the sustain window plus a margin (say 30 runs), then roll `MATCH` rows up to a per-key daily count and keep `MISMATCH`, `INSUFFICIENT_EVIDENCE` and `ONE_SIDE` rows indefinitely. Manifest rows are kept as long as any evaluation for the run exists, so a `completed` run always has provable scope. Not needed for Increment 2; needed before any multi-month deployment.
2. **Recovery timeout** for marking a `started` run as `failed`. Proposal: a parameter `run_stale_after_minutes` with a default of 60, applied by a small recovery function in Increment 2c.
3. **Source unavailability at manifest time.** Transaction A requires both sources. If one is unreachable, no run is created and the caller sees the error. Open question: do we want a `source_unreachable` state recorded for observability, or is the operator notification enough? Lean toward not recording it until operations tells us otherwise, so the evidence table only holds runs that had a fair chance to see everything.

## References

- `rules.py`: `incident_key`, `latest_prior_resolution`, `classify_discrepancies`, and the null-skip behavior pinned by `test_null_value_is_skipped_silently`.
- `resolve.py`: current resolution capture and chronology enforcement.
- `docs/decisions/0004-postgres-event-store-docker-compose.md`: the three-layer model and the open item this ADR closes.
- `db/models.py`, `db/writes.py`: Increment 1 tables and the write-boundary validation pattern this ADR extends.
- Review notes, revision 2: scope-completeness and canonical-identity feedback that produced revision 3.
