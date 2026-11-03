# Deal Desk Reconciliation

A small reconciliation engine for two systems that don't talk to each other — with an SOP, a human escape hatch, a bias toward silence, and a weekly rollup that measures whether the problems it finds are actually getting solved.

**→ [Live dashboard](https://sholayy.github.io/deal-desk-reconciler/output/dashboard.html)** &nbsp;·&nbsp; produced by one run of `python3 reconcile.py` against the mock exports in `/data/`.

## The problem

Deal Desk sits between a commercial system (Salesforce) and a delivery system (Mission Control). Both hold records about the same opportunities. Neither pushes updates to the other. Over time they drift, and nobody notices until a delivery team starts work on the wrong contract value or a VP looks at a report that doesn't match what's live.

Most scripted approaches to this fail for a specific reason: scripts that quietly "fix" disagreements create new untraceable corrections that erode trust. Scripts that flag everything create alert fatigue and get ignored. The real problem isn't comparing records — it's deciding what the engine is *allowed* to decide on its own, surfacing the rest in a way humans will actually act on, and measuring whether each class of problem is actually going away over time.

## Operating model

Reconciliation is one stage of a five-stage loop. Naming all five keeps the roles clear and keeps the project honest about where it does and does not sit.

1. **Prevent** — validate opportunity data, approvals, and handoff requirements at submission so bad data never enters either system.
2. **Detect** — compare system records and classify every disagreement into one of four buckets. (This engine's primary job.)
3. **Resolve** — apply the SOP's proposed corrections, or assign exceptions to the accountable owner. (This engine proposes; a write-back layer and human workflow close the loop.)
4. **Eliminate root cause** — change the validation, workflow, mapping, or policy responsible for recurring failures. This is where the five root-cause categories (`policy_ambiguity`, `workflow_gap`, `upstream_validation_gap`, `sync_lag_or_process_gap`, `pending_stabilization`) point.
5. **Verify prevention** — measure recurrence, exception volume, and handoff quality to confirm the fix actually worked. (The weekly rollup's "Changes since last week: closed / recurring / new" headline.)

An implemented control is not necessarily a successful control. The weekly rollup is where evidence of success lives.

## How this handles it

- Every field on an opportunity has a documented owner in the SOP (`contracts.md` + `contract.yaml`). The engine applies the SOP, not its own judgment.
- Disagreements get classified into four action buckets (plus a held bucket for recent changes). Two propose auto-corrections. Two go to a human with a specific action.
- Every discrepancy gets a root-cause category so the weekly rollup can group recurring incidents by the kind of structural fix each one needs.
- Per-bucket persistence thresholds filter out mid-sync lag (field discrepancies) and workflow lag (missing records). Field changes should propagate between systems in minutes; opening a new Mission Control project after a Salesforce approval is a human action that can take a day. One threshold for both would be too coarse.
- Three output surfaces: a daily Slack message, an incident-lifecycle weekly rollup, and an always-on JSON audit log. Each serves a different reader on a different cadence.
- The weekly rollup separates new, recurring, and closed incidents. Recurring is the design-problem signal; closed is the system reporting on its own progress toward obsolescence.

## Quick start

```bash
pip install -r requirements.txt
python3 reconcile.py
```

Outputs land in `/output/`:
- `slack/slack_YYYY-MM-DD.txt` — the daily Slack message (also posted to Slack when configured)
- `slack/weekly_YYYY-MM-DD.txt` — the weekly rollup (also posted to Slack when configured)
- `reconciliation_log.json` — the append-only audit log
- `dashboard.html` — the observability dashboard

Mock input data is in `/data/`. Both CSVs are hand-designed to trigger every scenario the engine handles.

### Optional: post to Slack

Set `SLACK_WEBHOOK_URL` in your environment to post both messages to a real Slack channel:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
python3 reconcile.py
```

The engine always writes the text files locally as the reproducible artifact. The webhook post is additive. If the post fails for any reason (bad URL, network, rate limit) the run completes anyway and prints a warning. The audit log remains the source of truth.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Eleven unit tests cover the classifier: ownership rules in both directions, recency rules in both directions, the persistence threshold on field discrepancies and on missing records, missing records in both directions, contract gaps, matching values that produce nothing, and null-value handling. The classifier is the one place where the SOP is applied, so it's the one place that most needs regression protection.

## What's in the box

```
reconcile.py              # the engine (single file, five functions)
contract.yaml             # machine-readable SOP
contracts.md              # human-readable SOP (the policy)
field_mapping.yaml        # Salesforce ↔ Mission Control field name translation
runbook.md                # human playbook for what to do when the engine flags
data/                     # mock Salesforce and Mission Control exports
output/                   # generated outputs (Slack messages, dashboard, JSON log)
tests/                    # pytest unit tests for the classifier
requirements.txt          # runtime dependencies
requirements-dev.txt      # runtime + pytest
```

## Design decisions worth naming

**The SOP lives in two files.** `contracts.md` is for humans — it carries the reasoning, and it's where policy decisions get negotiated. `contract.yaml` is for the code — same rules, machine-readable. Separating them means non-engineers can own policy without touching code.

**The engine proposes, it does not apply.** The classifier assigns `auto_correct` as a *decision* based on the SOP. Writing that decision back to Salesforce or Mission Control is a separate step that this prototype deliberately does not include. Dashboard and Slack language reflects this throughout: "corrections proposed" not "corrections applied."

**The engine refuses to auto-create missing records.** Opening a Mission Control record implies committing delivery resources and a start date. That's a human decision, not a data decision. The engine flags; humans act.

**Each discrepancy carries a root-cause category.** Five categories, one per bucket: `policy_ambiguity` (contract gap), `workflow_gap` (missing record), `upstream_validation_gap` (owned-field drift), `sync_lag_or_process_gap` (shared-field drift), `pending_stabilization` (held). The category points at the kind of structural fix each class of incident needs. The weekly rollup groups recurring incidents by category so a reviewer sees which category of fix would retire the most incidents at once.

**Per-bucket persistence thresholds, not one global number.** Field discrepancies and missing records represent different kinds of latency. A field update should propagate between systems in minutes. Opening a new Mission Control project after a Salesforce approval is a human action that can take a day. One threshold for both would create false corrections on fresh field edits or long holds on legitimately missing records. The current defaults (1h for fields, 24h for missing records) are informed guesses; in a real deployment they would be set at the 95th percentile of observed sync lag per bucket, measured empirically.

**Daily Slack post + weekly rollup, no operational dashboard.** Ops teams live in Slack. A separate dashboard as a task-management tool is a new habit nobody wants. The dashboard exists only as observability — a monitoring surface for people who want to understand the system, not operate it.

**The weekly rollup is organized by incident lifecycle, not observation count.** An incident is a unique `(opportunity_id, concept, bucket)` triple. One incident firing five days in a row is one incident with five observations, not five incidents. The rollup separates new, recurring (observed both weeks), and closed (last week but not this week). Direction (improving / stable / worsening) is the headline signal. This is the "verify prevention" stage of the operating model turned into a weekly text file.

**Each run has a unique `run_id`.** Repeat executions on the same day don't inflate dashboard or weekly counts. Downstream views filter by the current run or by the latest run per day.

## Where each surface lives in production

The three outputs serve three different workflows:

- **Slack** is the notification channel — where the team notices something needs attention.
- **Salesforce** is the operational resolution layer — where resolving a flag actually happens, as a custom object (`Reconciliation_Flag__c`) with owner, status, resolution notes. The engine's audit log feeds this.
- **The dashboard** is observability and audit — where ops leads and analysts monitor system health, drift patterns, and the SOP's completeness over time. It is a monitoring surface, not a task-management interface.

Keeping these separate means operators never adopt a new tool to do their work.

## What's deliberately not in the box

- No real integration — the engine reads flat files. In production, this would be an API pull or a direct warehouse connection.
- No role-based access on who can update the SOP. Contract changes would go through a code review workflow.
- No historical trend analysis beyond this week vs last week. Pattern analysis across months would be the next layer once the system has enough runs to be worth analyzing.
- No Slack bot with threading or acknowledgments — the engine posts via an Incoming Webhook when `SLACK_WEBHOOK_URL` is set. A bot that threads replies, tracks acknowledgments, and routes to the right responder is the next step but out of scope for this prototype.

## Upstream intake controls

The engine handles downstream drift. A complete solution closes the upstream gap: controls at submission so bad data never enters either system in the first place. Each proposed control below ties to a specific recurring flag category and a success metric so the controls can be prioritized by measured impact, not by instinct.

| Recurring problem (bucket) | Permanent corrective action | Success measure |
| --- | --- | --- |
| Missing Mission Control records (`workflow_gap`) | Automated handoff trigger that opens the Mission Control project when commercial approval status flips to Approved | % of approved deals with a confirmed delivery record within SLA |
| Inconsistent contract values (`sync_lag_or_process_gap`) | Controlled contract versioning with effective dates and an authoritative commercial source | Contract-value mismatch rate across the open-opportunity population |
| Unacknowledged handoffs | Explicit acknowledgment workflow with an acknowledgment SLA (see Process-design opportunities below) | % of handoffs acknowledged within SLA |
| Missing required information at submission (`upstream_validation_gap`) | Required-field validation rules on the Opportunity object for the four SOP-governed fields: contract value, commercial approval status, opportunity owner, handoff flag | First-pass handoff acceptance rate |
| Free-text drift in commercial fields (`upstream_validation_gap`) | Picklist constraints on stage and commercial approval status | Rate of SOP-vocabulary mismatches surfaced per week |
| Unclear field ownership (`policy_ambiguity`) | Resolve SOP ambiguity and name an accountable owner in `contract.yaml` | Repeat policy-gap incidents in the weekly rollup |

Together with this engine, those controls form the full lifecycle: intake validation prevents what it can, reconciliation catches what slips through, the audit log is the common record, and the weekly rollup measures whether the controls actually reduced recurrence.

## Process-design opportunities noticed during build

These are structural observations surfaced during this build. They're kept out of scope because they're SOP and data-model redesigns, not next-step engineering tasks. Worth raising in the first week on the job.

**A. The handoff flag should be a state machine, not a boolean match.**

The SOP currently compares `handoff_submitted` and `handoff_received`. These are different events, not two sides of the same fact. A better design is a lifecycle: `Not Submitted → Submitted → Acknowledged → Accepted`, with `Returned for Rework` as a separate status. A submitted-but-unacknowledged handoff is not a data-quality issue. It becomes an operational exception only once it exceeds an acknowledgment SLA. The current boolean comparison surfaces every unacknowledged handoff as drift, which is noise.

**B. Contract-value drift needs a true source of truth, not a recency rule.**

The current recency rule picks the system with the newer record-level timestamp. A newer timestamp does not prove the contract value itself was updated more recently. The stronger long-term fix is an approved contract version with an effective date and a single authoritative commercial source. Changes to contract value propagate from the authoritative source through a controlled update process. This engine's reconciliation then becomes a safety net rather than the primary mechanism for deciding which value is correct.

## Known limitations and next engineering steps

Named explicitly because the signal is seeing the gaps, not hiding them.

- **No write-back layer.** The engine classifies and proposes corrections based on the SOP. It does not currently update Salesforce or Mission Control. Dashboard and Slack labels reflect this ("proposed" not "applied"). Next step: a write-back module with explicit `execution_status` tracking (proposed → applied → failed) and dry-run mode as the default. The audit log is the source of truth that layer would read from.
- **Recency uses record-level timestamps.** See Process-design opportunity B. Field-level change history or an authoritative contract version source are both proper fixes. Until then, favor the ownership rule where ambiguity matters.
- **Missing field values are skipped silently.** When either side's value is `NaN`, the comparison is bypassed. That hides real data quality problems. The right fix: track skipped comparisons as a separate category and surface them in the weekly rollup.
- **Persistence thresholds are informed guesses.** The 1h / 24h / 4h defaults in `contract.yaml` are not empirically derived. In a real deployment they would be set at the 95th percentile of observed sync lag per bucket, measured from the audit log over the first few weeks of operation.
- **No resolution-notes workflow.** The audit log records what the engine saw, not what the human did next. Adding a `resolution_notes` field plus a small CLI for appending them would let the weekly rollup surface recurring flags with no captured root cause (highest priority to investigate) separately from recurring flags with notes (pattern worth analyzing).
- **Test coverage is classifier-only.** The classifier has unit tests, but `emit_outputs`, `emit_weekly_rollup`, and `emit_dashboard` don't yet. Next step: integration tests that run the engine against a fixture directory and assert on the output files.

## How I'd evolve it

Next step would be moving the file ingestion to an API-based pull so the engine runs without manual exports. After that, the write-back layer with dry-run default. After that, versioning the SOP in git so every rule change has an audit trail. After that, routing the flags into Salesforce as described above and sunsetting the markdown rollup.

## Guiding principle

Measure success by how few preventable discrepancies the business continues to create, not by how many the engine catches.
