# Deal Desk Reconciliation — Runbook

**Version:** 2026-10-28
**Owner:** Deal Desk Operations

This runbook names what to do when the reconciliation flags something. Each section covers one flag type. The structure is deliberately uniform: when you see this, check these things first, then act, then escalate if needed.

The goal of this runbook is to make itself shorter over time. Every flag resolved and documented is a case we don't have to think about again.

---

## Missing Mission Control record

**When you see this:** an opportunity exists in Salesforce with commercial approval complete, but no corresponding record exists in Mission Control. The reconciliation will not auto-create the record. Opening a Mission Control record implies committing delivery resources and a start date, which the system can't determine from Salesforce fields alone.

**Before pinging anyone, check:**

1. Is the opportunity actually still active in Salesforce? Sometimes a deal gets cancelled or deferred and the Salesforce stage didn't get updated. If that's the case, update the stage. The flag clears on the next run and nobody's time was spent.
2. Does the Salesforce record have everything a Mission Control record *should* have — approved contract value, scope, named owner, start date? If any of these are missing, this is a data hygiene issue on the Salesforce side first. Flag it back to the AE through the normal channels, don't push it to EPM yet.
3. Is this a recent approval (within 24 hours) and the system just caught the gap before someone had time to open the Mission Control record? If so, hold until the next run — this will likely resolve on its own.

**Only once all three are no:** this is a legitimate missing record that needs an EPM to open. Ping the assigned EPM (or if none assigned, Deal Desk to assign one) with the opportunity ID. Don't editorialize — the EPM knows what to do with a missing record.

**Escalation:** if the opportunity is high-value or urgent and no EPM responds within 48 hours, raise it in the next Deal Desk ops sync.

**Why these checks come first:** the point of this role is reducing noise, not producing it. If the system flags something that's really just stale data on the AE's side, pinging an EPM to open a record wastes their time *and* trains them to ignore future flags. Clean the data first, then escalate only what's genuinely a handoff gap.

---

## Contract gap

**The goal of this section is to disappear over time.** Every resolved contract gap closes a case the system will never need to flag again. If this section is still long a year from now, that's a signal we're not converting gaps into decisions fast enough.

**When you see this:** two systems disagree on a field, but `contract.yaml` doesn't name an owner for that field. The system cannot resolve this because there's no rule to apply yet.

**Before raising it:**

1. Check if the disagreement is actually meaningful — is one of the values obviously stale, obviously wrong, or just a formatting difference the field mapping missed? If it's a mapping issue (e.g. "USD 500000" vs "$500,000"), that's a one-line fix to `field_mapping.yaml`, not a contract decision.
2. Check whether this field is likely to come up again. If it's a one-off edge case on a weird opportunity, it may not need a permanent rule — a comment in the log explaining why we didn't add one is sometimes the right answer.
3. If it's a recurring field or likely to be, this needs a decision in `contract.yaml`.

**Only once it's confirmed as a real policy gap:** bring it to the next Deal Desk ops sync. Decide one of three things with the group: Salesforce owns it, Mission Control owns it, or shared (recency wins). Update `contract.yaml` with the new rule and the one-line rationale. Commit and re-run.

**Why this one matters more than others:** contract gaps compound. Every unresolved gap is a field the system permanently can't help with. If the same gap shows up for two consecutive weeks, it's no longer a question of "when do we get to it" — it's actively undermining the system. Prioritize these in your weekly review.

---

## Missing input file

The reconciliation couldn't run because an expected export wasn't in `/data/`. No reconciliation happened today; yesterday's state remains on the dashboard.

1. Check whether the export was generated at all. The export is currently manual, so this usually means someone forgot or an upstream automation failed.
2. If manual, generate and drop it in `/data/`. Re-run the reconciliation.
3. If this happens a second time within a week, treat it as a signal to prioritize moving the export to an API-based fetch. The manual process has already proven itself unreliable at that point; waiting for a third failure means living with a system that silently stops working.

**The real fix for this flag is to eliminate it entirely.** The sooner the export runs on an API pull with proper monitoring, the sooner this section of the runbook becomes obsolete. That's the goal.

---

If you encounter a flag or situation not covered here, raise it in the next Deal Desk ops sync and update the runbook with the resolution. The runbook is a living document — unclear or missing cases are the signal that it needs updating.