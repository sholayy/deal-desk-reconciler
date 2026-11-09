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
4. **Eliminate root cause** — change the validation, workflow, mapping, or policy responsible for recurring failures. This is where the five root-cause categories (`policy_ambiguity`, `workflow_gap`, `ownership_or_sync_gap`, `sync_lag_or_process_gap`, `pending_stabilization`) point.
5. **Verify prevention** — measure recurrence, exception volume, and handoff quality after an implemented fix. A quiet week is an encouraging signal but does not alone prove that a preventive action worked.

An implemented control is not necessarily a successful control. The weekly rollup provides early signals, but genuine verification requires documented remediation and enough post-fix monitoring to establish whether the failure mode recurs.

## How this handles it

- Every field on an opportunity has a documented owner in the SOP (`contracts.md` + `contract.yaml`). The engine applies the SOP, not its own judgment.
- Disagreements get classified into four action buckets (plus a held bucket for recent changes). Two propose auto-corrections. Two go to a human with a specific action.
- Every discrepancy gets a root-cause category so the weekly rollup can group recurring incidents by the kind of structural fix each one needs.
- Per-bucket persistence thresholds filter out mid-sync lag (field discrepancies) and workflow lag (missing records). Field changes should propagate between systems in minutes; opening a new Mission Control project after a Salesforce approval is a human action that can take a day. One threshold for both would be too coarse.
- Three output surfaces: a daily Slack message, an incident-lifecycle weekly rollup, and an always-on JSON audit log. Each serves a different reader on a different cadence.
- The weekly rollup separates new, recurring, and not-observed-this-week incidents. Recurrence is an investigation signal; absence alone does not establish a permanent fix.

## Quick start

```bash
pip install -r requirements.txt
python3 reconcile.py
```

Outputs land in `/output/`:
- `slack/slack_YYYY-MM-DD.txt` — the daily Slack message (also posted to Slack when configured)
- `slack/weekly_YYYY-MM-DD.txt` — the weekly rollup (also posted to Slack when configured)
- `reconciliation_log.json` — observation history plus resolution annotations (not immutable)
- `dashboard.html` — the observability dashboard

Mock input data is in `/data/`. Both CSVs are hand-designed to trigger every scenario the engine handles.

### Optional: post to Slack

Set `SLACK_WEBHOOK_URL` in your environment to post both messages to a real Slack channel:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
python3 reconcile.py
```

The engine always writes the text files locally as the reproducible artifact. The webhook post is additive. If the post fails for any reason (bad URL, network, rate limit) the run completes anyway and prints a warning. The audit log remains the source of truth.

### Capturing resolutions

When a flagged incident is actually resolved, the human appends a short record to the audit log so future runs can tell whether the fix held:

```bash
python3 resolve.py OPP-012 \
    --cause "Handoff Flow trigger did not fire for API-driven approvals" \
    --fix "Updated Flow trigger to listen for API events in addition to UI events" \
    --team "Deal Desk Automation"
```

If the same incident is observed after a documented resolution, the dashboard surfaces "Still detected after recorded resolution" and the weekly rollup labels the recurring incident similarly. Both surfaces display the confirmed root cause and corrective action the human captured. Neither claims the fix failed; a later observation is a signal to investigate (confirm the fix was deployed, look for a new triggering event), not a proven failure.

**Chronology is enforced.** The check requires valid ordering: `original observation < resolution < repeated observation`. A resolution dated before the original observation or after the current one cannot trigger a failed-fix warning. Incidents absent from a subsequent week are reported as not observed, not automatically verified as fixed.

**Demo flow — three commands, no YAML editing.** To demonstrate the full detect-fix-recur loop end to end against the mock data, use `--resolved-at` on the resolution and `--now` on the second reconciliation to override `simulated_now` in `contract.yaml`:

```bash
# 1. Initial reconciliation at simulated_now
python3 reconcile.py --now "2026-10-28T13:30:00Z"

# 2. Capture a resolution dated after the first run
python3 resolve.py OPP-012 \
    --resolved-at "2026-10-29T00:00:00Z" \
    --cause "Handoff Flow trigger did not fire for API-driven approvals" \
    --fix "Updated Flow trigger to listen for API events in addition to UI events" \
    --team "Deal Desk Automation"

# 3. Re-run at a later simulated time so the current observation post-dates the fix
python3 reconcile.py --now "2026-10-30T13:30:00Z"
```

Open `output/dashboard.html` and the OPP-012 card now carries the red "Still detected after recorded resolution" box with the resolution date, team, confirmed cause, corrective action, and a note that a later observation does not prove the fix failed.

## Generating larger synthetic datasets

The pinned CSVs under `data/` are 14 rows designed to trigger every classifier scenario once. For engineering work that needs volume (metrics with statistical power, failure-injection tests, Phase 2 integration testing), generate a larger dataset:

```bash
python3 -m data.generate --n 500 --seed 42 --output data/generated/
```

Output lands in `data/generated/` (gitignored, regenerate with the same seed to reproduce). The generator is deterministic given the seed and produces 500 opportunities with contract values drawn from a log-normal distribution, record ages from a gamma distribution, and intentional drift patterns (contract-value drift, missing MC records, contract gaps on handoff flags, owned-field drift) at configurable rates. See `docs/decisions/0003` for the distribution choices.

Both `reconcile.py` and the `POST /reconcile` API endpoint accept generated data unchanged because the output schema matches the pinned CSVs.

## Running the API

The business logic in `rules.py` and `handoff/eligibility.py` is exposed over HTTP by a FastAPI service for orchestrators that cannot or should not import Python source directly. Three endpoints:

- `GET /health` — operational readiness probe.
- `POST /events/opportunity/approved` — validates a Salesforce `CommercialApprovalApproved` event and returns a structured eligibility decision.
- `POST /reconcile` — accepts parallel lists of Salesforce and Mission Control records and returns the classified discrepancies.

Run it:

```bash
pip install -r requirements.txt
uvicorn api.main:app --reload
```

Then open `http://localhost:8000/docs` for the Swagger UI. The OpenAPI specification is at `/openapi.json`.

Design notes and alternatives considered live in `docs/decisions/0001` (why event validation and reconciliation are separate endpoints) and `docs/decisions/0002` (why FastAPI specifically).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Twenty tests across three files cover different layers of protection.

Eleven unit tests in `tests/test_classifier.py` cover specific classifier scenarios: ownership rules in both directions, recency rules in both directions, the persistence threshold on field discrepancies and on missing records, missing records in both directions, contract gaps, matching values that produce nothing, and null-value handling.

Six regression tests in `tests/test_recurrence.py` cover the incident-lifecycle logic: resolution chronology, true post-resolution recurrence, invalid dates, missing-record lifecycle identity, weekly warning accuracy, and simulated-run deduplication.

Three snapshot tests in `tests/test_regression.py` lock in the full classifier output against the project's mock inputs at a fixed `simulated_now`. If any future refactor changes what the classifier produces for these inputs, the snapshot test fails, so behavioral drift becomes a deliberate commit rather than an invisible regression. The baseline lives in `tests/snapshots/classifier_baseline.json` and is regenerated only when the output change is intended.

Fifteen handoff eligibility tests in `tests/test_handoff_eligibility.py` cover the rules implemented in `handoff/eligibility.py`: happy path, every rejection reason in the `EligibilityReason` enum, chronology edge cases (revocation of a different version, revocation before the approval, same-version supersession), rule ordering, and the timezone-naive boundary rejection.

Twelve API boundary tests in `tests/test_api.py` cover each endpoint and its error paths: health, happy-path eligibility and reconciliation, malformed payloads returning 422, timezone-naive timestamps rejected at the boundary, business-rule rejections returning 200 with a structured reason, and an OpenAPI schema check that catches undocumented endpoints.

Nine data generator tests in `tests/test_data_generator.py` verify the generator's contract: deterministic given a seed, different seeds produce different output, requested volume is honored exactly, drift rates are honored exactly, zero-drift produces a clean baseline, the schema matches what the classifier consumes, and generated data runs through the classifier without errors.

Total: 56 tests across six files.

## What's in the box

```
rules.py                  # pure business logic (classifier, root-cause map, chronology)
reconcile.py              # entry point that loads inputs, calls rules, writes outputs
resolve.py                # CLI for appending a resolution to an audit log entry
contract.yaml             # machine-readable SOP
contracts.md              # human-readable SOP (the policy)
field_mapping.yaml        # Salesforce ↔ Mission Control field name translation
runbook.md                # human playbook for what to do when the engine flags
events.md                 # design doc for the Salesforce event contract (three separated state machines)
handoff/                  # eligibility engine for Salesforce approval events
  schemas.py              #   Pydantic models for event envelope and payload shapes
  eligibility.py          #   pure business rules for handoff eligibility
api/                      # FastAPI service exposing the business logic as HTTP
  main.py                 #   /health, /events/opportunity/approved, /reconcile
  schemas.py              #   request/response Pydantic models for reconciliation
docs/decisions/           # Architecture Decision Records
  0001-...md              #   why /events/opportunity is split from /reconcile
  0002-...md              #   why FastAPI with these three endpoints
  0003-...md              #   synthetic dataset generator design
data/                     # mock Salesforce and Mission Control exports
  generate.py             #   parameterized synthetic dataset generator
  generated/              #   gitignored output dir for larger generated datasets
output/                   # generated outputs (Slack messages, dashboard, JSON log)
tests/                    # pytest unit tests for the classifier
requirements.txt          # runtime dependencies
requirements-dev.txt      # runtime + pytest
```

## Design decisions worth naming

**The SOP lives in two files.** `contracts.md` is for humans — it carries the reasoning, and it's where policy decisions get negotiated. `contract.yaml` is for the code — same rules, machine-readable. Separating them means non-engineers can own policy without touching code.

**Business logic is a module, not a script.** The classifier, the root-cause mapping, and the chronology helpers live in `rules.py` with no file I/O, no Slack delivery, no HTML generation, and no module-level globals. `reconcile.py` is one caller: it loads CSVs, runs the rules, writes outputs. Other callers (an n8n workflow calling a Python node, a FastAPI service, a scheduled job in another language shelling out to Python, a unit test fixture) can import `rules.py` directly and apply the SOP without pulling in the I/O layer. This is the architectural seam that keeps future orchestration work cheap.

**The engine proposes, it does not apply.** The classifier assigns `auto_correct` as a *decision* based on the SOP. Writing that decision back to Salesforce or Mission Control is a separate step that this prototype deliberately does not include. Dashboard and Slack language reflects this throughout: "corrections proposed" not "corrections applied."

**The engine refuses to auto-create missing records.** Opening a Mission Control record implies committing delivery resources and a start date. That's a human decision, not a data decision. The engine flags; humans act.

**Each discrepancy carries a *possible* root-cause category, not an established diagnosis.** Categories provide hypotheses for investigation: `policy_ambiguity` (contract gap), `workflow_gap` (missing record), `ownership_or_sync_gap` (owned-field drift), `sync_lag_or_process_gap` (shared-field drift), `pending_stabilization` (held). The category points at the kind of structural fix each class of incident needs. The weekly rollup groups recurring incidents by category so a reviewer sees which category of fix would retire the most incidents at once.

**Per-bucket persistence thresholds, not one global number.** Field discrepancies and missing records represent different kinds of latency. A field update should propagate between systems in minutes. Opening a new Mission Control project after a Salesforce approval is a human action that can take a day. One threshold for both would create false corrections on fresh field edits or long holds on legitimately missing records. The current defaults (1h for fields, 24h for missing records) are informed guesses; in a real deployment they would be set at the 95th percentile of observed sync lag per bucket, measured empirically.

**Daily Slack post + weekly rollup, no operational dashboard.** Ops teams live in Slack. A separate dashboard as a task-management tool is a new habit nobody wants. The dashboard exists only as observability — a monitoring surface for people who want to understand the system, not operate it.

**The weekly rollup is organized by incident lifecycle, not observation count.** Incidents use a normalized identity across a field/record moving from held to actionable, so stabilization does not look like one incident closing and another opening. One incident firing five days in a row has five observations, not five independent incidents. The rollup separates new, recurring (observed both weeks), and not observed this week. Direction is a preliminary signal, not proof of prevention. Replays using identical simulated timestamps retain the last appended run per day.

**Each run has a unique `run_id`.** Repeat executions on the same day don't inflate dashboard or weekly counts. Downstream views filter by the current run or by the latest run per day.

**Resolutions get captured through a CLI, not a UI.** The engine's audit log records what it saw. `resolve.py` records what the human did about it: confirmed root cause, corrective action, responsible team. A CLI (rather than a web form) matches how ops engineers actually work and keeps the system scriptable. The weekly rollup and dashboard both read resolution data to answer the real question: did the fix hold? A resolution followed by a **later observation** is a reason to revisit the proposed preventive action. The CLI updates resolution metadata on an observation (so the log is not strictly immutable); it does not edit the source systems.

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
| Missing required information at submission (`ownership_or_sync_gap`) | Required-field validation rules on the Opportunity object for the four SOP-governed fields: contract value, commercial approval status, opportunity owner, handoff flag | First-pass handoff acceptance rate |
| Free-text drift in commercial fields (`ownership_or_sync_gap`) | Picklist constraints on stage and commercial approval status | Rate of SOP-vocabulary mismatches surfaced per week |
| Unclear field ownership (`policy_ambiguity`) | Resolve SOP ambiguity and name an accountable owner in `contract.yaml` | Repeat policy-gap incidents in the weekly rollup |

Together with this engine, those controls form the full lifecycle: intake validation prevents what it can, reconciliation catches what slips through, the audit log is the common record, and the weekly rollup measures whether the controls actually reduced recurrence.

## Process-design opportunities noticed during build

These are structural observations surfaced during this build. They're kept out of scope because they're SOP and data-model redesigns, not next-step engineering tasks. Worth raising in the first week on the job.

**A. The handoff flag should be a state machine, not a boolean match.**

The SOP currently compares `handoff_submitted` and `handoff_received`. These are different events, not two sides of the same fact. A better design is a lifecycle (`Not Submitted → Submitted → Acknowledged → Accepted`, with `Returned for Rework` as its own status). An unacknowledged handoff is not a data-quality issue; it becomes an exception only once it exceeds an acknowledgment SLA.

**B. Contract-value drift needs a true source of truth, not a recency rule.**

The recency rule picks the system with the newer record-level timestamp, but a newer timestamp does not prove the contract value itself changed more recently. The stronger fix is an approved contract version with an effective date and a single authoritative commercial source. Reconciliation then becomes a safety net rather than the primary mechanism for deciding which value is correct.

## Known limitations and next engineering steps

Named explicitly because the signal is seeing the gaps, not hiding them.

- **No write-back layer.** The engine classifies and proposes corrections based on the SOP. It does not currently update Salesforce or Mission Control. Dashboard and Slack labels reflect this ("proposed" not "applied"). Next step: a write-back module with explicit `execution_status` tracking (proposed → applied → failed) and dry-run mode as the default. The audit log is the source of truth that layer would read from.
- **Recency uses record-level timestamps.** See Process-design opportunity B. Field-level change history or an authoritative contract version source are both proper fixes. Until then, favor the ownership rule where ambiguity matters.
- **Missing field values are skipped silently.** When either side's value is `NaN`, the comparison is bypassed. That hides real data quality problems. The right fix: track skipped comparisons as a separate category and surface them in the weekly rollup.
- **Test coverage is classifier-only.** The classifier has unit tests, but `emit_outputs`, `emit_weekly_rollup`, and `emit_dashboard` don't yet. Next step: integration tests that run the engine against a fixture directory and assert on the output files.

## Where this goes next

The architectural seam opened by `rules.py` points at a specific evolution path. Each step is deliberate, not reflexive.

- **Event contract first, orchestration second.** The design artifact is `events.md`, which specifies the `OpportunityStateChanged` event the engine would listen for once a real orchestration layer sits in front of it: payload schema, required fields, permitted state transitions, idempotency rules, ordering rules, failure modes, security considerations, and open questions for the real deployment. Defining the contract before picking the orchestrator keeps the orchestrator replaceable rather than coupled.
- **HTTP wrapper around `rules.py`.** A small FastAPI service that exposes the classifier as `POST /classify` and returns the structured decision list. This keeps the business engine behind a stable interface so downstream callers do not import Python source. Any orchestrator calls the API; the API calls `rules.py`.
- **Orchestrator (n8n, Temporal, or similar).** Receives Salesforce webhooks, validates the payload against the event contract, calls the HTTP wrapper, routes the decisions to Slack or an approval workflow. The orchestrator owns workflow state. The engine owns business decisions. Separation of concerns.
- **Write-back layer with dry-run default.** Named in Known limitations. Appropriate once the orchestrator is in place, since write-backs should be driven by an orchestrated workflow rather than a batch script.

Adopting all four turns this project from a reconciliation engine into a GTM Operations Automation System in the direction the later reviews pointed at. The step-by-step sequence keeps each addition reviewable and defensible on its own merits.

## Guiding principle

Measure success by how few preventable discrepancies the business continues to create, not by how many the engine catches.

## Historical and demo-time safeguards

`contract.yaml` intentionally pins a simulated reconciliation time (`2026-10-28`). `resolve.py` records the real UTC time at which a human records a resolution. A November resolution is **not evidence** of a failed fix in an October report. The dashboard and weekly rollup compare actual ISO timestamps and only label a fix as failed when there is a subsequent observation after the documented resolution.

The engine's observation history is append-oriented, but `resolve.py` annotates previously recorded entries with a resolution object. This is a pragmatic demo workflow rather than a tamper-proof append-only audit store. In production, separate append-only resolution events and verification outcomes from observation data, with actor identity and write permissions.

The current root-cause category is a **hypothesis** based on the discrepancy type. Confirmation requires investigation and the documented root cause captured through `resolve.py`. Preventive success should be evaluated with multiple follow-up runs and metrics, not assumed from one quiet reporting week.


## Engineering baseline audit (2026-11-08)

The repository includes an independently reviewed proposed database ADR at
`docs/decisions/0004-postgres-event-store-docker-compose.md`. It is **design only**;
no PostgreSQL schema, migrations or durable worker are implemented yet.

The API's reconciliation adapter must map Mission Control `commercial_contact`
to the commercial owner concept. `program_lead` is an independent delivery role.
The handoff eligibility engine blocks duplicate accepted commitments and prevents
same-version re-creation after return for rework. Neither in-memory checks nor
caller-supplied historical context guarantee production-safe authorization;
transactional uniqueness and trusted persisted history remain next-phase work.

The regression fixture belongs at `tests/snapshots/classifier_baseline.json`
so a clean clone runs the complete test suite. Run `python -m pytest -q`.
