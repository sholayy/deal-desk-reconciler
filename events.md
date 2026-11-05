# Event contract: `OpportunityStateChanged`

This document defines the Salesforce-side event that a future orchestrator would subscribe to in order to drive this engine in real time rather than on a daily batch. It is the design artifact that makes any downstream orchestrator (n8n, Temporal, AWS Lambda, Salesforce Flow HTTP callouts) replaceable rather than coupled.

The current engine runs against flat CSV exports. This contract describes how the same engine would react to live state changes once an orchestration layer sits in front of it.

## Why define the event contract first, before the orchestrator

The orchestrator is a tool. The contract is the design. If the contract is right, the orchestrator is swappable: today n8n, tomorrow Temporal, the year after a custom queue. If the contract is wrong, every orchestrator will inherit the same bugs and the migration cost between orchestrators is what makes the system brittle.

So: the event contract gets designed first and reviewed on its own merits. The orchestrator gets chosen afterward based on operational needs (cost, team familiarity, availability of the failure modes the contract names).

## Event

**Name**: `OpportunityStateChanged`

**Emitted by**: Salesforce Platform Events or an Apex trigger that posts to an HTTP endpoint, depending on which pattern the Salesforce org supports.

**Fired when**: any of the SOP-governed fields on an Opportunity changes value, OR the Opportunity's lifecycle state transitions (for example, `Pending Approval` to `Approved`).

**Received by**: the orchestration layer, which validates the payload against this contract, enriches it if needed, and calls the reconciliation engine's `POST /classify` endpoint for a decision.

## Payload schema

```json
{
  "event_id": "evt_01HG8K2M3N4P5Q6R7S8T9V0W1X",
  "event_type": "OpportunityStateChanged",
  "event_version": "1.0.0",
  "emitted_at": "2026-11-05T14:23:17.482Z",
  "source": {
    "system": "salesforce",
    "org_id": "00D6g000003ABCDEAG",
    "actor": {
      "user_id": "0056g000000XYZABC",
      "email": "ae@example.com"
    }
  },
  "opportunity": {
    "id": "0066g00000MOCK12",
    "external_id": "OPP-012",
    "stage": "Closed Won",
    "commercial_approval_status": "Approved",
    "contract_value_usd": 480000,
    "opportunity_owner": "priya.shah@example.com",
    "customer_name": "Northwind Technologies Inc.",
    "handoff_submitted": true,
    "last_modified_ts": "2026-11-05T14:23:17.000Z"
  },
  "transition": {
    "field": "commercial_approval_status",
    "from": "Pending Approval",
    "to": "Approved"
  }
}
```

### Required fields

Every event must carry, or the orchestrator rejects it with a 400:

- `event_id`: globally unique identifier for this event. Used for idempotency.
- `event_type`: fixed string, allows routing when multiple event types share a listener.
- `event_version`: semantic version of this contract. Consumers pin against a major version and tolerate minor.
- `emitted_at`: UTC ISO 8601 with timezone. The orchestrator rejects events without timezone information, same chronology rule the dashboard already applies.
- `opportunity.external_id`: the `OPP-xxx` identifier the rest of this system uses.
- `opportunity.last_modified_ts`: UTC ISO 8601 with timezone.
- `transition.field`: the field that changed, or `lifecycle` when the change was a stage transition.
- `transition.from` and `transition.to`: the before and after values. Null for creation events.

### Optional fields

Present when meaningful, absent otherwise. The orchestrator must not reject on absence:

- `source.actor`: who or what caused the change. Null when the change came from an automation.
- `opportunity.*` for fields the SOP does not govern: may be present for enrichment but the engine ignores them.

## Permitted state transitions

The engine should accept only transitions the SOP considers valid. Everything else is a data-integrity event, not a reconciliation event, and should route to a separate handler.

| From | To | Allowed | Action |
| --- | --- | --- | --- |
| `Draft` | `Pending Approval` | Yes | Enqueue for classifier |
| `Pending Approval` | `Approved` | Yes | Enqueue for classifier; trigger downstream handoff workflow |
| `Pending Approval` | `Rejected` | Yes | Enqueue for classifier |
| `Approved` | `Closed Won` | Yes | Enqueue for classifier |
| `Approved` | `Closed Lost` | Yes | Enqueue for classifier |
| `Approved` | `Pending Approval` | No | Reject with 409; route to data-integrity queue |
| `Closed Won` | any | No | Reject with 409; closed opportunities are immutable to the SOP |
| `Closed Lost` | `Pending Approval` | Flag only | Route to human review with context |

Transitions marked `No` indicate a bug upstream (someone bypassed the approval workflow, data migration drifted) or malicious activity. Route separately, do not run the classifier on them.

## Idempotency

The engine must handle duplicate events safely. Two patterns to combine:

1. **Deduplication by `event_id`**. The orchestrator records every processed event ID in a short-lived store (Redis, DynamoDB, a Postgres table with a TTL index). Repeated `event_id` within the TTL window is a no-op.
2. **Idempotent classifier call**. The engine's `POST /classify` is pure given its inputs. Repeated calls with the same payload return the same decision. The audit log entry includes `event_id` so repeated writes with the same ID either upsert or reject. Upsert is preferable: the latest write reflects the latest view.

TTL window for deduplication: 24 hours. Long enough to cover any reasonable orchestrator retry burst, short enough that the store stays small.

## Ordering

Events can arrive out of order when the network loses and retries. The engine must compare `emitted_at` against the audit log for the same `opportunity.external_id` and reject events older than the latest processed event for that opportunity. Old events do not overwrite newer decisions.

This is the same chronology rule the dashboard already applies to resolutions: `original observation < resolution < repeated observation`. The out-of-order case is `event_a < event_b < event_a_retry`; the engine should treat `event_a_retry` as a no-op because `event_b` is already the latest state.

## Failure modes

The orchestrator must name every failure mode and have a plan for each:

- **Classifier returns 5xx**: retry with exponential backoff, max 3 attempts, then route to a dead-letter queue with the full event payload for manual investigation.
- **Classifier returns 4xx**: the event payload is malformed. Do not retry; route directly to dead-letter with the validation error from the classifier.
- **Classifier times out**: same as 5xx. Retry policy applies.
- **Payload fails contract validation at the orchestrator**: reject with 400 to Salesforce, no retry. Salesforce retries happen at the Platform Events layer.
- **Duplicate event (same `event_id` within TTL)**: 200 OK with a `status: "deduplicated"` body. Do not call the classifier.
- **Out-of-order event (older than latest processed for this opportunity)**: 200 OK with a `status: "superseded"` body. Do not call the classifier.
- **Dead-letter queue depth exceeds threshold**: alert the on-call engineer. Growing DLQ depth means the classifier or an upstream system is broken.

Every failure mode logs to the same structured log the classifier writes to. The weekly rollup should include DLQ depth and failure-mode counts as operational health metrics, distinct from reconciliation business metrics.

## Security considerations

- **Authentication**: the orchestrator endpoint requires a signed JWT or an HMAC signature from Salesforce. No unauthenticated events. Rotate secrets quarterly.
- **PII**: the payload includes `opportunity.customer_name` and `source.actor.email`. Both are PII under most frameworks. The audit log must either exclude these or encrypt them at rest. The classifier itself does not need the actor email for the decision; strip it before calling the classifier.
- **Opportunity values**: `contract_value_usd` is commercially sensitive. Audit log access should be role-restricted and the metrics dashboard should show aggregates only, not per-opportunity values, outside of operational Slack messages to the Deal Desk team.
- **Replay attacks**: `event_id` deduplication protects against orchestrator-level replay. The HMAC signature protects against forged events.

## What this enables for the engine

Once the orchestrator is in place and this contract is honored:

- The classifier runs per-event, within seconds of the Salesforce change, instead of once per day on a batch.
- The audit log carries `event_id` and `emitted_at`, tying every decision to a specific traceable upstream event.
- The weekly rollup can distinguish between "incident detected" and "incident caused by a late-arriving event," giving the team cleaner signal.
- The write-back layer, when it lands, can post updates back to Salesforce through the same orchestrator rather than directly, which keeps all cross-system traffic routed through one auditable channel.

## Versioning

This document is version 1.0.0. The `event_version` field in the payload matches. Changes follow semantic versioning:

- **Patch** (1.0.x): documentation clarification, no schema change.
- **Minor** (1.x.0): additive fields, backward compatible. Consumers tolerate.
- **Major** (x.0.0): breaking changes (field removed, field renamed, allowed transitions narrowed). Consumers must opt in and the previous version keeps running until all consumers migrate.

The contract itself lives in git. Changes go through the same review as code. The SOP evolution loop applies here too: an event contract that keeps producing dead-letter entries is a contract problem, not a Salesforce problem.

## Open questions for the real deployment

Named explicitly so a future implementation phase has them in front of it, not behind it:

- Does the Salesforce org use Platform Events or an Apex-posted webhook? Determines whether the orchestrator subscribes or receives HTTP calls.
- What is the expected event rate at peak? Drives orchestrator sizing and the TTL window for deduplication.
- Who owns the dead-letter queue? Deal Desk ops, Salesforce admin, or a shared SRE channel?
- What is the retention policy for the audit log once `event_id` is in it? GDPR right-to-erasure applies to any field that identifies a person.
