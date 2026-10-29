# Deal Desk SOP: Field Ownership and Reconciliation Rules

**Version:** 2026-10-28
**Owner:** Deal Desk Operations

This SOP defines the shared vocabulary between Salesforce and Mission Control for an opportunity record: what each field means, which system owns it, and how disagreements are resolved. Both systems hold records about the same opportunities, but each uses different field names and different definitions. Without a shared definition, data drifts quietly and nobody can agree on which value is correct. This document is the agreement.

The reconciliation runs automatically against the rules below. It is not allowed to decide anything this document does not cover.

## Scope

Two systems hold records about the same opportunities:

- **Salesforce** — the commercial system of record, owned by GTM and Deal Desk.
- **Mission Control** — the delivery system of record, owned by EPM and Delivery.

Neither system pushes updates to the other. Over time they drift. The reconciliation reads from both daily, applies the rules below, and either auto-corrects or flags.

## Shared Key

Records are joined across systems using `opportunity_id` (Salesforce) and `mc_project_id` (Mission Control). The Mission Control ID is formatted as `MC-` + the Salesforce ID.

## Field Ownership

### Salesforce owns

| Field (Salesforce) | Field (Mission Control) | Rule | Why |
|---|---|---|---|
| `customer_name` | `account` | Ownership — Salesforce wins | Commercial record is the legal entity source. |
| `commercial_approval_status` | *(not present)* | Ownership — Salesforce only | Deal Desk approval decision; Mission Control does not touch this. |
| `opportunity_owner` | `commercial_contact` | Ownership — Salesforce wins | GTM owner is set in Salesforce and governed by Sales management. |
| `stage` | *(not present)* | Ownership — Salesforce only | Commercial lifecycle stage; Mission Control does not represent this. |

### Mission Control owns

| Field (Mission Control) | Field (Salesforce) | Rule | Why |
|---|---|---|---|
| `delivery_squad` | *(not present)* | Ownership — Mission Control only | EPM assigns delivery resourcing. |
| `kickoff_date` | *(not present)* | Ownership — Mission Control only | Delivery start is set by EPM at planning. |
| `project_status` | *(not present)* | Ownership — Mission Control only | Operational state of the engagement, owned by Delivery. |
| `program_lead` | *(not present)* | Ownership — Mission Control only | Named EPM responsible for the engagement. |

### Shared (both systems hold a value)

| Concept | Salesforce field | Mission Control field | Rule | Why |
|---|---|---|---|---|
| Contract value | `contract_value_usd` | `engagement_value` | Recency — most recent timestamp wins | AE may renegotiate post-approval; the more recent value is the current truth. |
| Last updated | `last_modified_ts` | `updated_at` | Metadata — used for recency decisions | Not a reconciled field; informs the rule above. |

### Contract gaps (knowingly unresolved)

| Concept | Salesforce field | Mission Control field | Why unresolved |
|---|---|---|---|
| Handoff flag | `handoff_submitted` | `handoff_received` | These are named similarly but mean different things — Salesforce indicates the commercial side has pushed the handoff; Mission Control indicates the delivery team has acknowledged receipt. The SOP has not yet decided whether these should be treated as the same field, mirrored, or kept separate. Until this is resolved, the reconciliation flags disagreements rather than auto-correcting. |

## Resolution Rules

Every discrepancy classifies into one of four buckets and is handled accordingly.

1. **Owned field, non-owner disagrees** — The non-owner is corrected silently and the action is logged with the rule that fired. No Slack notification. Visible in the weekly dashboard and the audit log.

2. **Shared field, both systems have different values** — The `last_updated` timestamp on each record decides. The older side is corrected silently and logged. No Slack notification.

3. **Record exists in one system but not the other** — No auto-create. Flags for human review in Slack. Reason: creating a delivery record implies committing resources, which is a human decision.

4. **Contract gap** — Fields disagree but the SOP has not named an owner. No auto-resolution. Flags in Slack with a note that the SOP needs updating. These are a priority signal for the Deal Desk ops sync.

## Persistence Threshold

No action is taken on any discrepancy where the most recent update on either side is less than 4 hours old. The reason: data entered minutes ago may not have propagated yet, and acting on it would create false corrections.

Discrepancies below the threshold are logged as "held for next run" and re-evaluated at the next run. The 4-hour threshold reflects the typical sync lag observed across the two systems. Reviewed quarterly.

## When Action Is Taken vs When It's Flagged

| Situation | Action | Slack notification |
|---|---|---|
| Owned field, non-owner stale | Auto-correct | None |
| Shared field, one side older | Auto-correct (recency wins) | None |
| Record missing from one system | None | Flag |
| Contract gap | None | Flag |
| Below persistence threshold | Hold for next run | None |
| Both systems agree | None | None |

## How This SOP Evolves

- Contract gaps are the signal that this document is incomplete. Every gap resolved in the Deal Desk ops sync becomes a new row in the Field Ownership tables above.
- Field mapping drift (e.g. a Mission Control field gets renamed) is handled in `field_mapping.yaml`, not here.
- Threshold adjustments or rule changes are tracked in the git history of this file. Every change should name the decision maker and the one-line rationale.

## Related Documents

- `contract.yaml` — machine-readable version of the ownership rules.
- `field_mapping.yaml` — the field name translation between Salesforce and Mission Control.
- `runbook.md` — the human playbook for what to do when a flag is raised.