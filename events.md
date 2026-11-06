# Event contracts: Opportunity lifecycle

This document defines the Salesforce-side events a future orchestrator would subscribe to in order to drive this engine in real time. The current engine runs against flat CSV exports; this contract describes how the same engine would react to live state changes once an orchestration layer sits in front of it.

**Design principle:** three separate state machines, three separate event families, one combined projection. Collapsing them into one mixed state transition table loses the distinction between what a commercial team controls (approval), what the CRM records (opportunity lifecycle), and what the delivery team owns (handoff acknowledgment). Keeping them separate makes each one testable in isolation and makes bugs easier to localize.

## Why separate state machines

Commercial approval, opportunity stage, and delivery handoff are owned by three different teams with three different workflows:

- **Commercial approval** is the Deal Desk decision about whether the terms are acceptable. Owned by Deal Desk / Finance.
- **Opportunity stage** is the CRM record of where the opportunity sits in the sales lifecycle. Owned by the account executive and Sales management.
- **Delivery handoff** is the Delivery organization's acknowledgment that they have received and accepted the engagement. Owned by Delivery / EPM.

Mixing them into one state machine forces bad trade-offs. An opportunity whose approval was revoked while the handoff acknowledgment was pending is a legitimate business state. A single flat enum cannot represent it.

## State machine 1: Commercial approval status

Owned by Deal Desk. Tracked on Salesforce as `commercial_approval_status`.

```
NotSubmitted → SubmittedForApproval → Approved
                                   ↘ Rejected → Resubmitted (loops back to SubmittedForApproval)
Approved → Revoked
```

### Permitted transitions

| From | To | Allowed | Who triggers |
| --- | --- | --- | --- |
| `NotSubmitted` | `SubmittedForApproval` | Yes | Account executive |
| `SubmittedForApproval` | `Approved` | Yes | Deal Desk approver |
| `SubmittedForApproval` | `Rejected` | Yes | Deal Desk approver |
| `Rejected` | `SubmittedForApproval` | Yes | Account executive (resubmission) |
| `Approved` | `Revoked` | Yes | Deal Desk approver (rare, serious) |
| `Revoked` | `SubmittedForApproval` | Yes | Account executive (resubmission after revocation) |
| Any other | Any other | No | Reject with 409; route to data-integrity queue |

A `Revoked → Approved` direct transition is explicitly forbidden. Resubmission must go through `SubmittedForApproval` so the approval record carries a fresh timestamp and approver identity.

### Events emitted

- `CommercialApprovalSubmitted`
- `CommercialApprovalApproved`
- `CommercialApprovalRejected`
- `CommercialApprovalRevoked`

## State machine 2: Opportunity stage

Owned by Sales. Tracked on Salesforce as `stage`.

```
Prospect → Qualifying → Pricing → PendingApproval → ClosedWon
                                                 ↘ ClosedLost
```

### Permitted transitions

| From | To | Allowed | Notes |
| --- | --- | --- | --- |
| `Prospect` | `Qualifying` | Yes | |
| `Qualifying` | `Pricing` | Yes | |
| `Pricing` | `PendingApproval` | Yes | Triggers commercial approval submission |
| `PendingApproval` | `ClosedWon` | Yes | Requires commercial approval already approved |
| `PendingApproval` | `ClosedLost` | Yes | |
| `PendingApproval` | `Pricing` | Yes | Approval rejected, back to pricing negotiation |
| `ClosedWon` | Anything | No | Closed won is immutable to the SOP |
| `ClosedLost` | `PendingApproval` | Flag only | Reopening a closed-lost deal is legitimate but requires human review |

### Events emitted

- `OpportunityStageChanged` (carries `from_stage` and `to_stage`)

## State machine 3: Delivery handoff status

Owned by Delivery / EPM. Tracked on Mission Control as `handoff_status`.

```
NotSubmitted → Submitted → Acknowledged → Accepted
                         ↘ ReturnedForRework → Submitted (loops back)
```

### Permitted transitions

| From | To | Allowed | Who triggers |
| --- | --- | --- | --- |
| `NotSubmitted` | `Submitted` | Yes | Deal Desk (post-approval handoff) |
| `Submitted` | `Acknowledged` | Yes | EPM assigned |
| `Submitted` | `ReturnedForRework` | Yes | EPM, when scope is incomplete |
| `Acknowledged` | `Accepted` | Yes | EPM, after validation |
| `Acknowledged` | `ReturnedForRework` | Yes | EPM, when issue found during validation |
| `ReturnedForRework` | `Submitted` | Yes | Deal Desk, after rework |
| `Accepted` | Anything | No | Accepted handoffs are immutable; delivery lifecycle takes over |

**Acknowledgment SLA:** `Submitted → Acknowledged` within 4 business hours. Breaches generate a separate operational exception event (`HandoffAcknowledgmentSLABreached`), not a reconciliation flag.

### Events emitted

- `HandoffSubmitted`
- `HandoffAcknowledged`
- `HandoffAccepted`
- `HandoffReturnedForRework`
- `HandoffAcknowledgmentSLABreached`

## Event payload schema (common envelope)

Every event carries the same envelope. The `payload` field changes shape by event type.

```json
{
  "event_id": "evt_01HG8K2M3N4P5Q6R7S8T9V0W1X",
  "event_type": "CommercialApprovalApproved",
  "event_version": "1.0.0",
  "emitted_at": "2026-11-05T14:23:17.482Z",
  "source": {
    "system": "salesforce",
    "org_id": "00D6g000003ABCDEAG",
    "actor": {
      "user_id": "0056g000000XYZABC",
      "email": "approver@example.com"
    }
  },
  "opportunity_id": "OPP-012",
  "payload": {
    "approved_contract_version": "v3",
    "contract_value_usd": 480000,
    "currency": "USD",
    "approval_notes": "Standard terms, no exceptions."
  }
}
```

### Common required fields

- `event_id`: globally unique. Idempotency key.
- `event_type`: fixed string from the list above.
- `event_version`: semantic version. Consumers pin major, tolerate minor.
- `emitted_at`: UTC ISO 8601 with timezone. No timezone means rejection.
- `opportunity_id`: the business identifier (`OPP-xxx`).
- `source.system`, `source.actor`: who/what caused the change.
- `payload`: event-type-specific body.

### Payload shapes by event type

**`CommercialApprovalApproved`**:
```json
{
  "approved_contract_version": "v3",
  "contract_value_usd": 480000,
  "currency": "USD",
  "approval_notes": "...",
  "opportunity_owner_id": "USR-029",
  "delivery_scope_summary": "12-week implementation..."
}
```
This payload is the trigger for handoff eligibility evaluation. All five fields are required for the opportunity to be eligible for handoff initiation.

**`CommercialApprovalRevoked`**:
```json
{
  "previously_approved_contract_version": "v3",
  "revocation_reason": "Terms renegotiated after approval."
}
```
A revocation received after a handoff has been submitted triggers a staleness check (the handoff should not have been initiated on a revoked approval).

**`OpportunityStageChanged`**:
```json
{
  "from_stage": "Pricing",
  "to_stage": "PendingApproval"
}
```

**`HandoffSubmitted`**:
```json
{
  "triggering_approval_event_id": "evt_01HG8K2...",
  "triggering_approved_contract_version": "v3",
  "delivery_scope_summary": "...",
  "assigned_squad_hint": "Squad A"
}
```
The `triggering_approval_event_id` links the handoff back to the approval it was generated from. This is the chain-of-custody record the reconciler and the auditor both read.

## Idempotency

Two layers:

1. **Deduplication by `event_id`**. The orchestrator records every processed event ID in a short-lived store (Redis or a `processed_events` table with a TTL index). Repeated `event_id` within the TTL window is a no-op.
2. **Idempotent business handlers**. Each handler (eligibility evaluation, reconciliation, acknowledgment) is pure given its inputs. Repeated calls with the same payload return the same decision. The audit table includes `event_id` so repeated writes with the same ID are rejected or upserted (upsert preferred so the latest write reflects the latest view).

TTL for deduplication: 24 hours.

## Ordering

Events can arrive out of order when the network loses and retries. Every handler compares `emitted_at` against the latest event already processed for the same `opportunity_id`. Older events for the same opportunity are a no-op with status `superseded`.

Special case: a `CommercialApprovalRevoked` that arrives after a `HandoffSubmitted` is NOT superseded by the handoff. It represents a genuine business state (the approval was revoked after the handoff was initiated) and must be recorded. The handoff is then flagged for review, not ignored.

## Failure modes

| Failure | Response | Retry | DLQ |
| --- | --- | --- | --- |
| Downstream handler returns 5xx | Retry with exponential backoff | Yes, max 3 | After retries exhausted |
| Downstream handler returns 4xx | Record the validation error, do not retry | No | Immediately |
| Downstream handler times out | Retry with exponential backoff | Yes, max 3 | After retries exhausted |
| Payload fails envelope validation | Reject at orchestrator with 400 | No | Not DLQ, surfaced to Salesforce |
| Duplicate event_id within TTL | Return 200 with `status: "deduplicated"` | No | No |
| Out-of-order event (older than latest processed) | Return 200 with `status: "superseded"` | No | No |
| DLQ depth exceeds threshold | Page on-call | n/a | Already in DLQ |

Dead-letter queue is a durable table in the operational database, not an in-memory list. Recovery across worker restarts is a core requirement.

## Security

- **Authentication**: endpoint requires a signed JWT or HMAC signature from Salesforce. No unauthenticated events. Secrets rotated quarterly.
- **PII**: `source.actor.email`, `opportunity_owner_id`, and any email fields in payloads are PII. The audit log must either exclude these or encrypt them at rest. Handlers that do not need the actor identity for the business decision strip it before processing.
- **Commercial sensitivity**: `contract_value_usd` is commercially sensitive. Audit log access is role-restricted. The metrics dashboard shows aggregates only; per-opportunity values go only to operational Slack channels for the Deal Desk team.
- **Replay attacks**: `event_id` deduplication protects against orchestrator-level replay. The HMAC signature protects against forged events.

## Versioning

Semantic versioning. The `event_version` field in the envelope matches.

- **Patch** (1.0.x): documentation clarification only.
- **Minor** (1.x.0): additive fields. Consumers tolerate.
- **Major** (x.0.0): breaking changes. Consumers opt in. The previous version keeps running until all consumers migrate.

The contract itself lives in git. Changes go through the same review as code.

## What this enables

Once the orchestrator is in place and this contract is honored:

- The handoff eligibility engine runs per-event, within seconds of the Salesforce approval, instead of discovering misalignment hours or days later during reconciliation.
- The audit log carries `event_id` and `emitted_at` and the chain from approval event → handoff submission → acknowledgment, tying every decision to a traceable upstream event.
- The weekly rollup can distinguish between "incident caused by late-arriving event" and "incident caused by missing control," which is better signal for root-cause investigation.
- The write-back layer, when it lands, posts updates back to Salesforce through the same orchestrator rather than directly, keeping all cross-system traffic routed through one auditable channel.

## Open questions for real deployment

- Does the Salesforce org use Platform Events or an Apex-posted webhook? Determines whether the orchestrator subscribes or receives HTTP calls.
- What is the expected peak event rate? Drives orchestrator sizing and TTL window.
- Who owns the dead-letter queue? Deal Desk ops, Salesforce admin, or shared SRE?
- Retention policy for the audit log once `event_id` is in it? GDPR right-to-erasure applies to any field identifying a person.
- Acknowledgment SLA of 4 business hours: is "business hours" the EPM's local timezone or a fixed office timezone? Answer determines whether the SLA breach timer logic is trivial or significant work.
