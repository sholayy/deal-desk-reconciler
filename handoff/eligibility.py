"""
Eligibility engine: given a CommercialApprovalApproved event, decide whether
the opportunity is eligible to initiate a delivery handoff.

This module implements the business rules the reviewer identified as the
Phase 1 deliverable. Each rule is a pure function. The orchestrator calls
evaluate_eligibility() and gets a structured EligibilityDecision back.

Design choices worth naming:

    Pure functions, no I/O. Every input is passed as an argument. No reads
    from a database, no HTTP calls. This lets tests be fast and lets the
    orchestrator decide where state lives (in Phase 1, in memory; in Phase 2,
    Postgres).

    Structured rejection reasons, not just booleans. Each rejection carries
    a stable EligibilityReason enum value so downstream metrics can measure
    the rate of each failure mode independently. The weekly rollup can then
    show "60% of rejections this week were STALE_APPROVAL, which points at
    a specific upstream control to fix" instead of just "27 rejections."

    First failure stops evaluation. We return the first rule that fails
    rather than aggregating all failures. This matches how an operator would
    investigate: fix one thing, resubmit, see what fails next. Aggregating
    all failures encourages fixing ten things at once, which produces lower
    quality fixes.

    Chronology enforced at the boundary. The rule that rejects stale
    approvals (an approval later revoked, or superseded by a newer approval)
    uses the same chronology discipline as rules.py: timestamps must carry
    timezone information, and events must be compared against the latest
    known state for the opportunity.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from handoff.schemas import (
    CommercialApprovalApprovedEvent,
    CommercialApprovalRevokedEvent,
    HandoffRecord,
    HandoffStatus,
)


# -------------------------------------------------------------------
# Decision model
# -------------------------------------------------------------------

class EligibilityReason(str, Enum):
    """
    Stable identifiers for each possible eligibility outcome. The orchestrator,
    the audit log, and the metrics layer all read from this enum so a
    rejection reason has the same identifier everywhere it appears.
    """
    ELIGIBLE = "eligible"
    MISSING_CONTRACT_VERSION = "missing_contract_version"
    MISSING_OWNER = "missing_owner"
    MISSING_DELIVERY_SCOPE = "missing_delivery_scope"
    INVALID_CONTRACT_VALUE = "invalid_contract_value"
    APPROVAL_REVOKED = "approval_revoked"
    APPROVAL_SUPERSEDED = "approval_superseded"
    HANDOFF_ALREADY_EXISTS = "handoff_already_exists"


@dataclass(frozen=True)
class EligibilityDecision:
    """
    The structured result of evaluating eligibility for one approval event.

    `eligible` is the boolean verdict. `reason` is the stable identifier for
    why. `details` is a human-readable explanation suitable for the audit
    log and operational Slack messages. `event_id` and `opportunity_id` are
    carried through so downstream consumers do not need to zip the decision
    back to its originating event.
    """
    eligible: bool
    reason: EligibilityReason
    details: str
    event_id: str
    opportunity_id: str


# -------------------------------------------------------------------
# Individual rules
#
# Each rule returns an EligibilityDecision. ELIGIBLE means this rule passed
# and evaluation should continue to the next rule. Any other reason means
# this rule failed and evaluation should stop here.
# -------------------------------------------------------------------

def _check_payload_completeness(event: CommercialApprovalApprovedEvent) -> EligibilityDecision:
    """
    Verify the approval payload carries every field the SOP requires for
    handoff eligibility. Pydantic has already enforced that the fields are
    present and have the right types; this rule checks for business-level
    emptiness (a whitespace-only string is structurally valid but not
    meaningfully populated).

    Returns ELIGIBLE if every required field is non-empty, otherwise the
    specific MISSING_* reason for the first empty field found.
    """
    payload = event.payload

    if not payload.approved_contract_version.strip():
        return EligibilityDecision(
            eligible=False,
            reason=EligibilityReason.MISSING_CONTRACT_VERSION,
            details=(
                "The approved_contract_version field is empty. "
                "Handoff cannot be initiated without an identifiable contract version "
                "so the Delivery team knows which terms they are committing to."
            ),
            event_id=event.event_id,
            opportunity_id=event.opportunity_id,
        )

    if not payload.opportunity_owner_id.strip():
        return EligibilityDecision(
            eligible=False,
            reason=EligibilityReason.MISSING_OWNER,
            details=(
                "The opportunity_owner_id field is empty. "
                "Handoff cannot be initiated without a named commercial owner "
                "because the Delivery team needs someone to contact for scope questions."
            ),
            event_id=event.event_id,
            opportunity_id=event.opportunity_id,
        )

    if not payload.delivery_scope_summary.strip():
        return EligibilityDecision(
            eligible=False,
            reason=EligibilityReason.MISSING_DELIVERY_SCOPE,
            details=(
                "The delivery_scope_summary field is empty. "
                "Handoff cannot be initiated without a scope the Delivery team can act on."
            ),
            event_id=event.event_id,
            opportunity_id=event.opportunity_id,
        )

    return EligibilityDecision(
        eligible=True,
        reason=EligibilityReason.ELIGIBLE,
        details="Payload completeness check passed.",
        event_id=event.event_id,
        opportunity_id=event.opportunity_id,
    )


def _check_contract_value(event: CommercialApprovalApprovedEvent) -> EligibilityDecision:
    """
    Verify the approved contract value is a positive number. A zero or
    negative value is structurally valid at the Pydantic layer but
    represents either a data entry error or a pro bono engagement that
    should be flagged for human review, not auto-handed off.
    """
    if event.payload.contract_value_usd <= 0:
        return EligibilityDecision(
            eligible=False,
            reason=EligibilityReason.INVALID_CONTRACT_VALUE,
            details=(
                f"Approved contract value is {event.payload.contract_value_usd:.2f} USD, "
                f"which is not a positive amount. Handoff requires a positive contract value. "
                f"If this is a pro bono engagement or a correction, submit it through the "
                f"exception-handling workflow instead of the standard handoff path."
            ),
            event_id=event.event_id,
            opportunity_id=event.opportunity_id,
        )

    return EligibilityDecision(
        eligible=True,
        reason=EligibilityReason.ELIGIBLE,
        details="Contract value check passed.",
        event_id=event.event_id,
        opportunity_id=event.opportunity_id,
    )


def _check_approval_not_revoked(
    event: CommercialApprovalApprovedEvent,
    revocation_events: list[CommercialApprovalRevokedEvent],
) -> EligibilityDecision:
    """
    Verify no revocation of this specific approved_contract_version has
    arrived between the approval event and now.

    A revocation that post-dates the approval and names the same
    approved_contract_version means the approval is no longer current.
    Initiating a handoff on a revoked approval would commit the delivery
    team to terms the commercial team no longer agrees to. This is the
    chronology-sensitive rule the reviewer specifically called out.
    """
    approval_version = event.payload.approved_contract_version
    for revocation in revocation_events:
        if revocation.opportunity_id != event.opportunity_id:
            continue
        if revocation.payload.previously_approved_contract_version != approval_version:
            continue
        if revocation.emitted_at > event.emitted_at:
            return EligibilityDecision(
                eligible=False,
                reason=EligibilityReason.APPROVAL_REVOKED,
                details=(
                    f"Approval of contract version {approval_version} at "
                    f"{event.emitted_at.isoformat()} was revoked at "
                    f"{revocation.emitted_at.isoformat()}. Reason given: "
                    f"\"{revocation.payload.revocation_reason}\". "
                    f"Handoff cannot be initiated on a revoked approval."
                ),
                event_id=event.event_id,
                opportunity_id=event.opportunity_id,
            )

    return EligibilityDecision(
        eligible=True,
        reason=EligibilityReason.ELIGIBLE,
        details="Approval has not been revoked.",
        event_id=event.event_id,
        opportunity_id=event.opportunity_id,
    )


def _check_approval_not_superseded(
    event: CommercialApprovalApprovedEvent,
    later_approval_events: list[CommercialApprovalApprovedEvent],
) -> EligibilityDecision:
    """
    Verify no newer approval for a different contract version has arrived
    for the same opportunity after this event. If a newer approval exists
    for a different version, this older approval represents stale state
    and should not trigger a handoff.

    Approvals of the same version that arrive later are duplicate deliveries
    of the same event (idempotency) and do not supersede.
    """
    for later in later_approval_events:
        if later.opportunity_id != event.opportunity_id:
            continue
        if later.emitted_at <= event.emitted_at:
            continue
        if later.payload.approved_contract_version == event.payload.approved_contract_version:
            continue
        return EligibilityDecision(
            eligible=False,
            reason=EligibilityReason.APPROVAL_SUPERSEDED,
            details=(
                f"Approval of contract version {event.payload.approved_contract_version} "
                f"was superseded by a later approval of version "
                f"{later.payload.approved_contract_version} at "
                f"{later.emitted_at.isoformat()}. Handoff should be initiated against "
                f"the newer approval, not this older one."
            ),
            event_id=event.event_id,
            opportunity_id=event.opportunity_id,
        )

    return EligibilityDecision(
        eligible=True,
        reason=EligibilityReason.ELIGIBLE,
        details="Approval has not been superseded.",
        event_id=event.event_id,
        opportunity_id=event.opportunity_id,
    )


def _check_no_active_handoff(
    event: CommercialApprovalApprovedEvent,
    existing_handoffs: list[HandoffRecord],
) -> EligibilityDecision:
    """
    Verify no active handoff already exists for this opportunity. A handoff
    whose current_status is Accepted already represents an established delivery
    commitment and must block duplicate creation. ReturnedForRework blocks duplicate initiation for the same approved
    contract version, but a newly approved version can begin a replacement.
    """
    blocking_statuses = {HandoffStatus.SUBMITTED, HandoffStatus.ACKNOWLEDGED, HandoffStatus.ACCEPTED}
    for record in existing_handoffs:
        if record.opportunity_id != event.opportunity_id:
            continue
        if record.current_status not in blocking_statuses:
            # A returned handoff may be superseded by a new approved version,
            # but resubmitting the same contract version should amend the
            # existing handoff instead of creating a second one.
            if not (record.current_status == HandoffStatus.RETURNED_FOR_REWORK and
                    record.triggering_approved_contract_version == event.payload.approved_contract_version):
                continue
        return EligibilityDecision(
            eligible=False,
            reason=EligibilityReason.HANDOFF_ALREADY_EXISTS,
            details=(
                f"A handoff or delivery commitment for this opportunity already exists "
                f"(status: {record.current_status.value}, submitted at "
                f"{record.submitted_at.isoformat()}, triggered by approval event "
                f"{record.triggering_approval_event_id}). Initiating a second handoff "
                f"would create a duplicate delivery commitment. If the first handoff "
                f"needs rework, return it; if it was abandoned, close it before "
                f"re-approving."
            ),
            event_id=event.event_id,
            opportunity_id=event.opportunity_id,
        )

    return EligibilityDecision(
        eligible=True,
        reason=EligibilityReason.ELIGIBLE,
        details="No active handoff exists for this opportunity.",
        event_id=event.event_id,
        opportunity_id=event.opportunity_id,
    )


# -------------------------------------------------------------------
# Public entry point
# -------------------------------------------------------------------

def evaluate_eligibility(
    event: CommercialApprovalApprovedEvent,
    existing_handoffs: Optional[list[HandoffRecord]] = None,
    revocation_events: Optional[list[CommercialApprovalRevokedEvent]] = None,
    later_approval_events: Optional[list[CommercialApprovalApprovedEvent]] = None,
) -> EligibilityDecision:
    """
    Evaluate every eligibility rule in order of increasing specificity.
    Return the first failing rule's decision, or ELIGIBLE if all rules pass.

    Rule order rationale:
        1. Payload completeness: cheapest to check, catches the most common
           errors (missing fields in a hand-crafted integration).
        2. Contract value: cheap, catches data-entry bugs.
        3. Revocation: requires historical event scan, but a revoked approval
           is the most urgent business-level problem so it takes priority
           over staleness from supersession.
        4. Supersession: similar history scan; catches the case where a
           newer approval exists for the same opportunity.
        5. Active handoff: the dedupe check; ordered last so a rejection
           from here means every other rule has passed and the problem is
           purely idempotency.

    All historical arguments default to empty lists, which models a fresh
    deployment with no prior state. Tests use this default to construct
    focused scenarios.
    """
    existing_handoffs = existing_handoffs or []
    revocation_events = revocation_events or []
    later_approval_events = later_approval_events or []

    for rule in (
        lambda: _check_payload_completeness(event),
        lambda: _check_contract_value(event),
        lambda: _check_approval_not_revoked(event, revocation_events),
        lambda: _check_approval_not_superseded(event, later_approval_events),
        lambda: _check_no_active_handoff(event, existing_handoffs),
    ):
        decision = rule()
        if not decision.eligible:
            return decision

    return EligibilityDecision(
        eligible=True,
        reason=EligibilityReason.ELIGIBLE,
        details=(
            f"Opportunity {event.opportunity_id} is eligible to initiate a handoff. "
            f"Approved contract version: {event.payload.approved_contract_version}. "
            f"Contract value: {event.payload.contract_value_usd:.2f} "
            f"{event.payload.currency}. Owner: {event.payload.opportunity_owner_id}."
        ),
        event_id=event.event_id,
        opportunity_id=event.opportunity_id,
    )
