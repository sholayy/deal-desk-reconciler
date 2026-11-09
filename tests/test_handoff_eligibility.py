"""
Tests for the handoff eligibility engine.

The reviewer defined the Phase 1 acceptance test as "an approved opportunity
with valid information is eligible for handoff; an incomplete or unauthorized
request cannot advance." These tests encode that acceptance test plus the
specific failure modes each eligibility rule is responsible for.

Rule coverage required: one happy-path test, one test per rejection reason
in EligibilityReason, plus a chronology-sensitive test for stale approvals.
Each test constructs a focused fixture so a failure points at a specific
rule, not an interaction.
"""

from datetime import datetime, timedelta, timezone

import pytest

from handoff.eligibility import (
    EligibilityDecision,
    EligibilityReason,
    evaluate_eligibility,
)
from handoff.schemas import (
    CommercialApprovalApprovedEvent,
    CommercialApprovalApprovedPayload,
    CommercialApprovalRevokedEvent,
    CommercialApprovalRevokedPayload,
    EventSource,
    HandoffRecord,
    HandoffStatus,
)


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------

BASE_TIME = datetime(2026, 11, 5, 14, 23, 17, tzinfo=timezone.utc)


def _make_approval(
    opportunity_id: str = "OPP-012",
    event_id: str = "evt_001",
    emitted_at: datetime = BASE_TIME,
    approved_contract_version: str = "v3",
    contract_value_usd: float = 480000,
    currency: str = "USD",
    approval_notes: str = "Standard terms.",
    opportunity_owner_id: str = "USR-029",
    delivery_scope_summary: str = "12-week implementation.",
) -> CommercialApprovalApprovedEvent:
    """A valid approval event. Overrides let a test construct a variant quickly."""
    return CommercialApprovalApprovedEvent(
        event_id=event_id,
        event_version="1.0.0",
        emitted_at=emitted_at,
        source=EventSource(
            system="salesforce",
            org_id="00D6g000003ABCDEAG",
            actor=None,
        ),
        opportunity_id=opportunity_id,
        payload=CommercialApprovalApprovedPayload(
            approved_contract_version=approved_contract_version,
            contract_value_usd=contract_value_usd,
            currency=currency,
            approval_notes=approval_notes,
            opportunity_owner_id=opportunity_owner_id,
            delivery_scope_summary=delivery_scope_summary,
        ),
    )


def _make_revocation(
    opportunity_id: str = "OPP-012",
    previously_approved_contract_version: str = "v3",
    emitted_at: datetime = BASE_TIME + timedelta(hours=2),
    revocation_reason: str = "Terms renegotiated after approval.",
) -> CommercialApprovalRevokedEvent:
    return CommercialApprovalRevokedEvent(
        event_id=f"revoke_{opportunity_id}",
        event_version="1.0.0",
        emitted_at=emitted_at,
        source=EventSource(
            system="salesforce",
            org_id="00D6g000003ABCDEAG",
            actor=None,
        ),
        opportunity_id=opportunity_id,
        payload=CommercialApprovalRevokedPayload(
            previously_approved_contract_version=previously_approved_contract_version,
            revocation_reason=revocation_reason,
        ),
    )


# -------------------------------------------------------------------
# Happy path
# -------------------------------------------------------------------

def test_valid_approval_is_eligible():
    """
    The reviewer's acceptance test: an approved opportunity with valid
    information is eligible for handoff. This is the baseline every other
    test varies from.
    """
    decision = evaluate_eligibility(_make_approval())
    assert decision.eligible is True
    assert decision.reason == EligibilityReason.ELIGIBLE
    assert decision.opportunity_id == "OPP-012"
    assert decision.event_id == "evt_001"


# -------------------------------------------------------------------
# Payload completeness failures
# -------------------------------------------------------------------

def test_empty_contract_version_rejected():
    """
    Pydantic accepts an empty string as a valid str value; business-level
    emptiness is the eligibility engine's job to reject.
    """
    decision = evaluate_eligibility(_make_approval(approved_contract_version="   "))
    assert decision.eligible is False
    assert decision.reason == EligibilityReason.MISSING_CONTRACT_VERSION


def test_empty_delivery_scope_rejected():
    """Pydantic has a min_length=1 on this field, so empty strings are rejected
    at the schema layer. This test exercises the whitespace-only case that
    Pydantic accepts but the eligibility rule should still reject."""
    # The schema rejects truly empty; whitespace passes schema, eligibility rejects.
    decision = evaluate_eligibility(_make_approval(delivery_scope_summary="   "))
    assert decision.eligible is False
    assert decision.reason == EligibilityReason.MISSING_DELIVERY_SCOPE


# -------------------------------------------------------------------
# Contract value failures
# -------------------------------------------------------------------

def test_zero_contract_value_rejected():
    decision = evaluate_eligibility(_make_approval(contract_value_usd=0.0))
    assert decision.eligible is False
    assert decision.reason == EligibilityReason.INVALID_CONTRACT_VALUE


def test_negative_contract_value_rejected():
    decision = evaluate_eligibility(_make_approval(contract_value_usd=-100.0))
    assert decision.eligible is False
    assert decision.reason == EligibilityReason.INVALID_CONTRACT_VALUE


# -------------------------------------------------------------------
# Chronology-sensitive failures
# -------------------------------------------------------------------

def test_revoked_approval_rejected():
    """
    The reviewer's stale-approval case: an approval that was later revoked
    must not trigger a handoff. This is the chronology rule applied to
    eligibility, matching the discipline the dashboard applies to resolutions.
    """
    approval = _make_approval()
    revocation = _make_revocation(
        previously_approved_contract_version="v3",
        emitted_at=approval.emitted_at + timedelta(hours=2),
    )

    decision = evaluate_eligibility(approval, revocation_events=[revocation])
    assert decision.eligible is False
    assert decision.reason == EligibilityReason.APPROVAL_REVOKED
    assert "Terms renegotiated" in decision.details


def test_revocation_for_different_version_does_not_apply():
    """A revocation of v2 should not block a later approval of v3."""
    approval = _make_approval(approved_contract_version="v3")
    old_revocation = _make_revocation(
        previously_approved_contract_version="v2",
        emitted_at=approval.emitted_at - timedelta(days=1),
    )

    decision = evaluate_eligibility(approval, revocation_events=[old_revocation])
    assert decision.eligible is True


def test_revocation_before_approval_does_not_apply():
    """A revocation that predates the current approval is historical; the
    later approval supersedes it."""
    approval = _make_approval(emitted_at=BASE_TIME)
    old_revocation = _make_revocation(
        previously_approved_contract_version="v3",
        emitted_at=BASE_TIME - timedelta(hours=5),
    )

    decision = evaluate_eligibility(approval, revocation_events=[old_revocation])
    assert decision.eligible is True


def test_superseded_approval_rejected():
    """
    An approval that a newer approval of a different contract version has
    overtaken is stale state. The handoff should be initiated against the
    newer version, not this one.
    """
    old_approval = _make_approval(approved_contract_version="v2")
    newer_approval = _make_approval(
        event_id="evt_002",
        approved_contract_version="v3",
        emitted_at=old_approval.emitted_at + timedelta(hours=3),
    )

    decision = evaluate_eligibility(
        old_approval, later_approval_events=[newer_approval]
    )
    assert decision.eligible is False
    assert decision.reason == EligibilityReason.APPROVAL_SUPERSEDED


def test_same_version_later_approval_is_not_supersession():
    """Receiving the same approval again (duplicate delivery) is not supersession;
    it is idempotency, which is handled by event_id deduplication, not here."""
    approval = _make_approval(approved_contract_version="v3")
    duplicate = _make_approval(
        event_id="evt_002",
        approved_contract_version="v3",
        emitted_at=approval.emitted_at + timedelta(hours=1),
    )

    decision = evaluate_eligibility(approval, later_approval_events=[duplicate])
    assert decision.eligible is True


# -------------------------------------------------------------------
# Duplicate handoff rejection
# -------------------------------------------------------------------

def test_existing_active_handoff_rejected():
    """
    If an active handoff already exists for this opportunity, the approval
    event should not create a second handoff. Returning this reason tells
    the operator the problem is idempotency, not data quality.
    """
    approval = _make_approval()
    existing = HandoffRecord(
        opportunity_id="OPP-012",
        triggering_approval_event_id="earlier_evt",
        triggering_approved_contract_version="v3",
        submitted_at=approval.emitted_at - timedelta(hours=5),
        current_status=HandoffStatus.SUBMITTED,
    )

    decision = evaluate_eligibility(approval, existing_handoffs=[existing])
    assert decision.eligible is False
    assert decision.reason == EligibilityReason.HANDOFF_ALREADY_EXISTS


def test_returned_handoff_is_not_active():
    """A handoff that was returned for rework is not active; a new approval
    can legitimately initiate a replacement."""
    approval = _make_approval()
    returned = HandoffRecord(
        opportunity_id="OPP-012",
        triggering_approval_event_id="earlier_evt",
        triggering_approved_contract_version="v2",
        submitted_at=approval.emitted_at - timedelta(days=3),
        current_status=HandoffStatus.RETURNED_FOR_REWORK,
    )

    decision = evaluate_eligibility(approval, existing_handoffs=[returned])
    assert decision.eligible is True


def test_handoff_for_different_opportunity_does_not_block():
    """A handoff that exists for a different opportunity ID should not
    block this opportunity's handoff."""
    approval = _make_approval(opportunity_id="OPP-012")
    other = HandoffRecord(
        opportunity_id="OPP-099",
        triggering_approval_event_id="other_evt",
        triggering_approved_contract_version="v3",
        submitted_at=approval.emitted_at - timedelta(hours=1),
        current_status=HandoffStatus.SUBMITTED,
    )

    decision = evaluate_eligibility(approval, existing_handoffs=[other])
    assert decision.eligible is True


# -------------------------------------------------------------------
# Rule ordering
# -------------------------------------------------------------------

def test_first_failure_wins():
    """
    When multiple rules would fail, the engine returns the first in the
    documented evaluation order. This encodes the operator workflow: fix
    one thing, resubmit, see what fails next.

    Payload completeness is evaluated first, so a missing contract version
    reason takes priority over an invalid contract value reason even if
    both conditions are true.
    """
    approval = _make_approval(
        approved_contract_version="   ",
        contract_value_usd=-100.0,
    )
    decision = evaluate_eligibility(approval)
    assert decision.eligible is False
    assert decision.reason == EligibilityReason.MISSING_CONTRACT_VERSION


# -------------------------------------------------------------------
# Schema boundary (envelope-level validation)
# -------------------------------------------------------------------

def test_timezone_naive_emitted_at_rejected_by_schema():
    """
    The chronology discipline from events.md requires timezone information
    on every timestamp. Pydantic enforces this at the schema boundary so
    eligibility logic never sees a naive datetime.
    """
    with pytest.raises(Exception):
        _make_approval(emitted_at=datetime(2026, 11, 5, 14, 23, 17))  # No tzinfo


@pytest.mark.parametrize("state", [HandoffStatus.ACCEPTED, HandoffStatus.RETURNED_FOR_REWORK])
def test_existing_commitment_or_rework_cannot_start_new_handoff(state):
    """An accepted or returned handoff must not spawn a second project."""
    approval = _make_approval()
    prior = HandoffRecord(
        opportunity_id=approval.opportunity_id,
        triggering_approval_event_id="evt_prior",
        triggering_approved_contract_version="v3",
        submitted_at=BASE_TIME,
        current_status=state,
    )
    decision = evaluate_eligibility(approval, existing_handoffs=[prior])
    assert not decision.eligible
    assert decision.reason == EligibilityReason.HANDOFF_ALREADY_EXISTS
