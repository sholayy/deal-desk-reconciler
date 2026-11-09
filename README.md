# Deal Desk Reconciler

A reconciliation engine for two operational systems that do not sync. Instead of the usual script that quietly "fixes" disagreements (and erodes trust) or flags everything (and gets ignored), the engine applies a documented SOP, classifies every disagreement into a specific action bucket, routes decisions to automation or a named human owner, and tags each discrepancy with a root-cause category so the weekly rollup points at structural fixes rather than endless detection.

*Measure success by how few preventable discrepancies the business continues to create, not by how many the engine catches.*

The engine proposes corrections. It does not apply them. The write-back layer exists in design but not in code.

[→ Live dashboard](./output/dashboard.html) · produced by one run against the pinned mock data in `/data/`.

## The problem

Deal Desk sits between a commercial system (Salesforce) and a delivery system (Mission Control). Both hold records about the same opportunities. Neither pushes updates to the other. Over time they drift, and nobody notices until a delivery team starts work on the wrong contract value or a VP looks at a report that does not match what is live.

Most scripted approaches fail for a specific reason. Scripts that silently fix disagreements create untraceable corrections that erode trust. Scripts that flag everything produce alert fatigue and get ignored. The real problem is not comparing records. It is deciding what the engine should decide on its own, surfacing the rest in a way humans will actually act on, and measuring whether each class of problem actually goes away over time.

## How it works

### Operating model

Reconciliation is one stage of a five-stage loop. Naming all five keeps the project honest about where it sits and where it does not.

1. **Prevent**: validate opportunity data, approvals, and handoff requirements at submission so bad data never enters either system.
2. **Detect**: compare system records and classify every disagreement. This engine's primary job.
3. **Resolve**: apply the SOP's proposed corrections or assign exceptions to the accountable owner. This engine proposes; a write-back layer and human workflow close the loop.
4. **Eliminate root cause**: change the validation, workflow, mapping, or policy responsible for recurring failures. The engine tags every discrepancy with one of five root-cause categories so this stage has a target.
5. **Verify prevention**: measure recurrence after a fix. A quiet week is an encouraging signal. It alone does not prove the fix worked.

An implemented control is not a verified control. The weekly rollup gives early signals; genuine verification requires documented remediation and enough post-fix observation to establish non-recurrence.

### Classification

Every disagreement lands in one of five buckets. Two propose auto-corrections. Two route to a human with a specific action. One holds while the SOP's persistence window elapses.

| Bucket | Trigger | Action |
| --- | --- | --- |
| `auto_correct_ownership` | SOP names one system as owner of the field | Engine proposes correction to the non-owning system |
| `auto_correct_recency` | SOP marks the field as shared; one side is clearly fresher | Engine proposes the newer value wins |
| `flag_contract_gap` | SOP has not resolved who owns the field | Human escalation with the open policy question named |
| `flag_missing_record` | One system has a record the other does not | Human escalation; engine refuses to create records |
| `hold` | Change is younger than the SOP's persistence threshold for its bucket | No action; recheck on the next run |

Each discrepancy also carries one of five root-cause categories: `policy_ambiguity`, `workflow_gap`, `ownership_or_sync_gap`, `sync_lag_or_process_gap`, `pending_stabilization`. The category is a hypothesis for investigation, not an established diagnosis. The weekly rollup groups recurring incidents by category so a reviewer sees which structural fix would retire the most incidents at once.

### Outputs

Three surfaces for three different readers on three different cadences.

- **Slack (daily)**: the notification channel. Where the team notices something needs attention.
- **Weekly rollup**: incident lifecycle view. New, recurring, and not-observed-this-week incidents grouped by root-cause category. Direction is a preliminary signal, not a proof of prevention.
- **Dashboard (always on)**: observability and audit. Monitoring surface for ops leads and analysts who want to understand drift patterns and SOP completeness over time. It is a monitoring surface, not a task-management interface.

Keeping these separate means operators never adopt a new tool to do their work. Resolutions still happen in Salesforce; Slack catches their attention; the dashboard answers "is this getting better."

## Engineering principles

### Business logic is a module, not a script

The classifier, root-cause mapping, and chronology helpers live in `rules.py` with no file I/O, no Slack delivery, no HTML generation, and no module-level globals. `reconcile.py` is one caller: it loads CSVs, runs the rules, writes outputs. Other callers (the FastAPI service, a scheduled job in another language, a unit test fixture) import `rules.py` directly and apply the SOP without pulling in the I/O layer. This is the architectural seam that keeps future orchestration work cheap.

### The engine proposes, it does not apply

The classifier assigns `auto_correct` as a decision based on the SOP. Writing that decision back to Salesforce or Mission Control is a separate step this prototype deliberately does not include. Dashboard and Slack language reflects this throughout: "corrections proposed," not "corrections applied."

### The engine refuses to auto-create missing records

Opening a Mission Control record implies committing delivery resources and a start date. That is a human decision, not a data decision. The engine flags; humans act.

### The SOP lives in two files, for two audiences

`contracts.md` is for humans. It carries the reasoning and is where policy gets negotiated. `contract.yaml` is for the code. Same rules, machine-readable. Separating them means non-engineers own policy without touching code.

### Configuration loads at the application boundary, not on every call

Field mapping and classification rules load once from `field_mapping.yaml` and `contract.yaml` into a validated `Config` object. The API, the batch reconciler, and the synthetic data generator all pull from the same validated source. Before this pattern landed, each caller hardcoded its own subset; two of those subsets drifted and shipped field-mapping bugs. See `AUDIT_NOTES.md` for the full history.

### Per-bucket persistence thresholds, not one global number

A field update should propagate between systems in minutes. Opening a new Mission Control project after a Salesforce approval is a human action that can take a day. One threshold for both would create false corrections on fresh field edits or long holds on legitimately missing records. The current defaults (1h for fields, 24h for missing records) are informed guesses; a real deployment fits them to the 95th percentile of observed sync lag per bucket.

### Incident identity survives bucket transitions

A field in `hold` that becomes actionable is the same incident, not a new one. Each incident's identity combines `opportunity_id` and `field`, so the weekly rollup separates true new incidents from stabilization transitions and reports direction honestly.

### Each run has a unique `run_id`

Repeat executions on the same day do not inflate dashboard or weekly counts. Downstream views filter by the current run or by the latest run per day.

## What is live vs what is designed

### Live

- `rules.py`: pure classifier, root-cause mapping, chronology enforcement.
- `reconcile.py`: batch entry point. Reads CSVs, runs the rules, writes Slack messages, dashboard, and audit log.
- `resolve.py`: CLI for appending a resolution to an audit log entry.
- `handoff/`: eligibility engine for Salesforce approval events. Pydantic schemas for the event envelope and payload shapes; pure business rules for duplicate protection and chronology.
- `api/`: FastAPI service exposing `/health`, `/events/opportunity/approved`, and `/reconcile`.
- `data/generate.py`: parameterized synthetic data generator with reproducible seeds and controlled drift rates.
- `config.py`: validated loader for the two YAML sources of truth, used by the batch loader, the API, and the generator.
- Dashboard and weekly rollup as static HTML generated per run.
- 67+ tests across `tests/`.

### Designed, not yet implemented

- PostgreSQL event store with Alembic migrations, Docker Compose for local development. See `docs/decisions/0004-postgres-event-store-docker-compose.md`.
- Incident lifecycle projection separating operator-claimed resolution, observation-confirmed absence, independently-verified correction, and prevention-validated states. ADR in draft.
- Write-back layer with explicit `execution_status` tracking (proposed → applied → failed) and dry-run default.
- Orchestration layer (n8n, Temporal, or similar) receiving Salesforce webhooks and routing decisions.

### Not planned at this stage

- Real Salesforce or Mission Control integration. The engine reads flat files in this build.
- Role-based access on who can update the SOP.
- Historical trend analysis beyond this week vs last week.
- Slack bot with threading and acknowledgments. The current build posts via Incoming Webhook only.

## Known limitations

Named explicitly because the signal is seeing the gaps.

- **No write-back.** The engine classifies and proposes. It does not update Salesforce or Mission Control. The audit log is the source of truth the write-back layer would read from.
- **Recency uses record-level timestamps.** A newer record timestamp does not prove the contract value itself changed more recently. The stronger fix is field-level change history or an authoritative contract version source.
- **The engine silently skips missing field values.** When either side is NaN, it bypasses the comparison entirely. The right fix is to track skipped comparisons as a separate category and surface them in the weekly rollup.
- **No multi-tenant namespace.** Opportunity IDs and event IDs would collide across organizations. Single-tenant demo only; a future ADR covers the namespacing.
- **No authentication on the API.** Intentional for local development. A future ADR must land before any non-local deployment.
- **Simulated time, not wall clock.** `contract.yaml` pins a `simulated_now` so tests and demos are reproducible. A production deployment unpins this.

## Upstream intake controls

The engine handles downstream drift. A complete solution closes the upstream gap: controls at submission so bad data never enters either system in the first place. Each proposed control below ties to a recurring flag category and a success measure, so the team prioritizes the controls by measured impact, not instinct.

| Recurring problem | Permanent corrective action | Success measure |
| --- | --- | --- |
| Missing Mission Control records (`workflow_gap`) | Automated handoff trigger opening the Mission Control project when commercial approval flips to Approved | % of approved deals with a confirmed delivery record within SLA |
| Inconsistent contract values (`sync_lag_or_process_gap`) | Controlled contract versioning with effective dates and an authoritative commercial source | Contract-value mismatch rate across the open-opportunity population |
| Unacknowledged handoffs | Explicit acknowledgment workflow with an acknowledgment SLA (see Process-design opportunities below) | % of handoffs acknowledged within SLA |
| Missing required information at submission (`ownership_or_sync_gap`) | Required-field validation rules on the Opportunity object for the four SOP-governed fields | First-pass handoff acceptance rate |
| Free-text drift in commercial fields (`ownership_or_sync_gap`) | Picklist constraints on stage and commercial approval status | Rate of SOP-vocabulary mismatches surfaced per week |
| Unclear field ownership (`policy_ambiguity`) | Resolve SOP ambiguity and name an accountable owner in `contract.yaml` | Repeat policy-gap incidents in the weekly rollup |

Together with this engine, those controls form the full lifecycle: intake validation prevents what it can, reconciliation catches what slips through, the audit log is the common record, and the weekly rollup measures whether the controls actually reduced recurrence.

## Process-design opportunities raised during build

Structural observations surfaced while building the engine. They are SOP and data-model redesigns, not next engineering steps, so they stay out of scope here. Worth raising in the first week on the job that adopts this.

**The handoff flag should be a state machine, not a boolean match.** Today the SOP compares `handoff_submitted` and `handoff_received`. These are different events, not two sides of the same fact. A lifecycle (`NotSubmitted` → `Submitted` → `Acknowledged` → `Accepted`, with `ReturnedForRework` as its own state) models the reality. An unacknowledged handoff is not a data quality issue until it exceeds an acknowledgment SLA.

**Contract-value drift needs a source of truth, not a recency rule.** The recency rule picks the system with the newer record-level timestamp. A newer timestamp does not prove the contract value itself changed more recently. The stronger fix is an approved contract version with an effective date and a single authoritative commercial source. Reconciliation then becomes a safety net rather than the primary mechanism for deciding which value is correct.

