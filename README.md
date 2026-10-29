# Deal Desk Reconciliation

A small reconciliation engine for two systems that don't talk to each other — with an SOP, a human escape hatch, and a bias toward silence.

## The problem

Deal Desk sits between a commercial system (Salesforce) and a delivery system (Mission Control). Both hold records about the same opportunities. Neither pushes updates to the other. Over time they drift, and nobody notices until a delivery team starts work on the wrong contract value or a VP looks at a report that doesn't match what's live.

Most scripted approaches to this fail for a specific reason: scripts that quietly "fix" disagreements create new untraceable corrections that erode trust. Scripts that flag everything create alert fatigue and get ignored. The real problem isn't comparing records — it's deciding what the engine is *allowed* to decide on its own, and surfacing the rest in a way humans will actually act on.

## How this handles it

- Every field on an opportunity has a documented owner in the SOP (`contracts.md` + `contract.yaml`). The engine applies the SOP, not its own judgment.
- Disagreements get classified into four buckets. Two are auto-corrected silently and logged. Two go to a human with a specific action.
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

Mock input data is in `/data/`. Both CSVs are hand-designed to trigger every scenario the engine handles.

## What's in the box

```
reconcile.py              # the engine (single file, five functions)
contract.yaml             # machine-readable SOP
contracts.md              # human-readable SOP (the policy)
field_mapping.yaml        # Salesforce ↔ Mission Control field name translation
runbook.md                # human playbook for what to do when the engine flags
data/                     # mock Salesforce and Mission Control exports
output/                   # generated outputs (Slack messages, JSON log)
```

## Design decisions worth naming

**The SOP lives in two files.** `contracts.md` is for humans — it carries the reasoning, and it's where policy decisions get negotiated. `contract.yaml` is for the code — same rules, machine-readable. Separating them means non-engineers can own policy without touching code.

**The engine refuses to auto-create missing records.** Opening a Mission Control record implies committing delivery resources and a start date. That's a human decision, not a data decision. The engine flags; humans act.

**The persistence threshold exists because systems lag.** A field updated 20 minutes ago may not have propagated yet. Acting on it would create false corrections and erode trust. Threshold is 4 hours, reviewed quarterly.

**Daily Slack post + weekly rollup, no dashboard.** Ops teams live in Slack. A separate dashboard is a new tool someone has to adopt and remember. The daily post surfaces what needs action; the weekly rollup surfaces what's trending. Both land where the team already works.

## Where this would live in production

The weekly surface in production wouldn't be a markdown rollup — it'd route into a custom object in Salesforce (`Reconciliation_Flag__c`), each flag a record with an owner, status, and resolution notes. Deal Desk already lives in Salesforce; access controls are already governed; the resolution path stays in the tool where the work happens. The engine already produces the data (`reconciliation_log.json`); the Salesforce automation that consumes it is a next-step build worth designing with the admin team, not in isolation.

## What's deliberately not in the box

- No real integration — the engine reads flat files. In production, this would be an API pull or a direct warehouse connection.
- No role-based access on who can update the SOP. Contract changes would go through a code review workflow.
- No historical trend analysis beyond this week vs last week. Pattern analysis across months would be the next layer once the system has enough runs to be worth analyzing.
- No actual Slack bot — the daily and weekly posts write to text files for the demo. In production this would use a webhook (one function call) or a bot with threading and acknowledgments.

## How I'd evolve it

Next step would be moving the file ingestion to an API-based pull so the engine runs without manual exports. After that, versioning the SOP in git so every rule change has an audit trail. After that, routing the flags into Salesforce as described above and sunsetting the markdown rollup.

