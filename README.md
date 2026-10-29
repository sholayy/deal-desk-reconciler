# Deal Desk Reconciliation

A small reconciliation engine for two systems that don't talk to each other — with an SOP, a human check, and a bias toward silence.

## The problem

Deal Desk sits between a commercial system (Salesforce) and a delivery system (Mission Control). Both hold records about the same opportunities. Neither pushes updates to the other. Over time they drift, and nobody notices until a delivery team starts work on the wrong contract value or a VP looks at a report that doesn't match what's live.

Most scripted approaches to this fail for a specific reason: scripts that quietly "fix" disagreements create new untraceable corrections that erode trust. Scripts that flag everything create alert fatigue and get ignored. The real problem isn't comparing records — it's deciding what the engine is *allowed* to decide on its own, and surfacing the rest in a way humans will actually act on.

## How this handles it

- Every field on an opportunity has a documented owner in the SOP (`contracts.md` + `contract.yaml`). The engine applies the SOP, not its own judgment.
- Disagreements get classified into four buckets. Two propose auto-corrections (logged, not written back in this prototype). Two go to a human with a specific action.
- A persistence threshold filters out mid-sync lag so the engine doesn't act on temporary drift.
- Three output surfaces: a daily Slack message, a weekly rollup, and an always-on JSON audit log. Each serves a different reader on a different cadence.

## Quick start

```bash
pip install -r requirements.txt
python reconcile.py
```

Outputs land in `/output/`:
- `slack/slack_YYYY-MM-DD.txt` — the daily Slack message
- `slack/weekly_YYYY-MM-DD.txt` — the weekly rollup
- `reconciliation_log.json` — the append-only audit log
- `dashboard.html` — the observability dashboard

Mock input data is in `/data/`. Both CSVs are hand-designed to trigger every scenario the engine handles.

## What's in the box

```
reconcile.py              # the engine (single file, five functions)
contract.yaml             # machine-readable SOP
contracts.md              # human-readable SOP (the policy)
field_mapping.yaml        # Salesforce ↔ Mission Control field name translation
runbook.md                # human playbook for what to do when the engine flags
data/                     # mock Salesforce and Mission Control exports
output/                   # generated outputs (Slack messages, dashboard, JSON log)
```

## Design decisions worth naming

**The SOP lives in two files.** `contracts.md` is for humans — it carries the reasoning, and it's where policy decisions get negotiated. `contract.yaml` is for the code — same rules, machine-readable. Separating them means non-engineers can own policy without touching code.

**The engine proposes, it does not apply.** The classifier assigns `auto_correct` as a *decision* based on the SOP. Writing that decision back to Salesforce or Mission Control is a separate step that this prototype deliberately does not include — see the limitations below. Dashboard and Slack language reflects this throughout: "corrections proposed" not "corrections applied."

**The engine refuses to auto-create missing records.** Opening a Mission Control record implies committing delivery resources and a start date. That's a human decision, not a data decision. The engine flags; humans act.

**The persistence threshold exists because systems lag.** A field updated 20 minutes ago may not have propagated yet. Acting on it would create false corrections and erode trust. The same threshold applies to missing records: an opportunity created in Salesforce an hour ago has not had time to open its Mission Control counterpart, so it is held rather than flagged. Threshold is 4 hours, reviewed quarterly.

**Daily Slack post + weekly rollup, no operational dashboard.** Ops teams live in Slack. A separate dashboard as a task-management tool is a new habit nobody wants. The dashboard exists only as observability — a monitoring surface for people who want to understand the system, not operate it.

**Each run has a unique `run_id`.** Repeat executions on the same day don't inflate dashboard or weekly counts — downstream views filter by the current run or by the latest run per day.

## Where each surface lives in production

The three outputs serve three different workflows:

- **Slack** is the notification channel — where the team notices something needs attention.
- **Salesforce** is the operational resolution layer — where resolving a flag actually happens, as a custom object (`Reconciliation_Flag__c`) with owner, status, resolution notes. The engine's audit log feeds this.
- **The dashboard** is observability and audit — where ops leads and analysts monitor system health, drift patterns, and the SOP's completeness over time. It is a monitoring surface, not a task-management interface.

Keeping these separate means operators never adopt a new tool to do their work. The dashboard is for people who want to understand the system, not operate it.

## What's deliberately not in the box

- No real integration — the engine reads flat files. In production, this would be an API pull or a direct warehouse connection.
- No role-based access on who can update the SOP. Contract changes would go through a code review workflow.
- No historical trend analysis beyond this week vs last week. Pattern analysis across months would be the next layer once the system has enough runs to be worth analyzing.
- No actual Slack bot — the daily and weekly posts write to text files for the demo. In production this would use a webhook (one function call) or a bot with threading and acknowledgments.

## Known limitations and next engineering steps

Named explicitly because the signal is seeing the gaps, not hiding them.

- **No write-back layer.** The engine classifies and proposes corrections based on the SOP. It does not currently update Salesforce or Mission Control. Dashboard and Slack labels reflect this ("proposed" not "applied"). Next step: a write-back module with explicit `execution_status` tracking (proposed → applied → failed) and dry-run mode as the default. The audit log is the source of truth that layer would read from.
- **Recency uses record-level timestamps.** Comparing `last_modified_ts_sf` and `last_modified_ts_mc` doesn't tell you which specific field changed most recently. A newer owner update doesn't mean the contract value is newer. Field-level change history is the proper fix. Until then, treat recency decisions as directional and favor the ownership rule where ambiguity matters.
- **Missing field values are skipped silently.** When either side's value is `NaN`, the comparison is bypassed. That hides real data quality problems. The right fix: track skipped comparisons as a separate category and surface them in the weekly rollup.
- **No automated tests.** The mock CSVs are useful test fixtures. First test coverage would be: missing records in both directions, matching fields, ownership rules, recency ties, missing values, timestamp boundary conditions, repeat runs, contract gaps, run_id dedupe.

## How I'd evolve it

Next step would be moving the file ingestion to an API-based pull so the engine runs without manual exports. After that, the write-back layer with dry-run default. After that, versioning the SOP in git so every rule change has an audit trail. After that, routing the flags into Salesforce as described above and sunsetting the markdown rollup.
