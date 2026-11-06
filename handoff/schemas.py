"""
Pydantic schemas for the Salesforce-side events defined in events.md.

These schemas validate the envelope and the payload shapes. Validation here
happens at the API boundary (before any business rule runs) so eligibility
and reconciliation logic never receive structurally invalid data.

The schemas intentionally do not enforce business rules. A payload with a
contract_value_usd of zero or a negative value will validate structurally
here; eligibility.py decides whether that value makes the opportunity
eligible for handoff. That split keeps the responsibilities clean: Pydantic
is a type system, not a policy engine.
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


# -------------------------------------------------------------------
# State machine enums (mirrors events.md)
# -------------------------------------------------------------------

class CommercialApprovalStatus(str, Enum):
    """States for commercial approval. Owned by Deal Desk."""
    NOT_SUBMITTED = "NotSubmitted"
    SUBMITTED_FOR_APPROVAL = "SubmittedForApproval"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    REVOKED = "Revoked"


class OpportunityStage(str, Enum):
    """States for the Salesforce opportunity stage. Owned by Sales."""
    PROSPECT = "Prospect"
    QUALIFYING = "Qualifying"
    PRICING = "Pricing"
    PENDING_APPROVAL = "PendingApproval"
    CLOSED_WON = "ClosedWon"
    CLOSED_LOST = "ClosedLost"


class HandoffStatus(str, Enum):
    """States for the delivery handoff. Owned by Delivery / EPM."""
    NOT_SUBMITTED = "NotSubmitted"
    SUBMITTED = "Submitted"
    ACKNOWLEDGED = "Acknowledged"
    ACCEPTED = "Accepted"
    RETURNED_FOR_REWORK = "ReturnedForRework"


# -------------------------------------------------------------------
# Envelope
# -------------------------------------------------------------------

class EventActor(BaseModel):
    """Who or what caused the event. Null for system-driven events."""
    model_config = ConfigDict(extra="forbid")

    user_id: str
    email: str


class EventSource(BaseModel):
    """The system that emitted the event."""
    model_config = ConfigDict(extra="forbid")

    system: Literal["salesforce", "mission_control"]
    org_id: str
    actor: Optional[EventActor] = None


class EventEnvelope(BaseModel):
    """
    The common envelope every event carries. The payload field is validated
    separately by the event-type-specific model so this base can stay thin.
    """
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., description="Globally unique identifier for this event. Idempotency key.")
    event_type: str
    event_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    emitted_at: datetime = Field(..., description="UTC ISO 8601. Must carry timezone.")
    source: EventSource
    opportunity_id: str = Field(..., pattern=r"^OPP-\d+$")

    @field_validator("emitted_at")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        """
        Enforce the chronology discipline from events.md: timestamps without
        timezone information are not evidence and must be rejected at the
        boundary. Treating them as UTC by default hides bugs.
        """
        if v.tzinfo is None:
            raise ValueError(
                "emitted_at must include a timezone. "
                "Timezone-naive timestamps are rejected at the boundary."
            )
        return v


# -------------------------------------------------------------------
# Payloads by event type
# -------------------------------------------------------------------

class CommercialApprovalApprovedPayload(BaseModel):
    """
    The payload for a successful commercial approval. All five fields here
    are required for the resulting opportunity to be eligible for handoff
    initiation. eligibility.py rejects events whose payload lacks any of
    them with a specific MISSING_* reason code.
    """
    model_config = ConfigDict(extra="forbid")

    approved_contract_version: str = Field(..., description="Immutable identifier for the specific contract version that was approved.")
    contract_value_usd: float = Field(..., description="Approved contract value in USD. Must be positive.")
    currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    approval_notes: str
    opportunity_owner_id: str = Field(..., pattern=r"^USR-\d+$")
    delivery_scope_summary: str = Field(..., min_length=1)


class CommercialApprovalApprovedEvent(EventEnvelope):
    """Full event: envelope plus approval-specific payload."""
    event_type: Literal["CommercialApprovalApproved"] = "CommercialApprovalApproved"
    payload: CommercialApprovalApprovedPayload


class CommercialApprovalRevokedPayload(BaseModel):
    """
    Payload for an approval revocation. Carries the previously approved
    contract version so downstream handlers can match the revocation to
    the specific approval it voids.
    """
    model_config = ConfigDict(extra="forbid")

    previously_approved_contract_version: str
    revocation_reason: str = Field(..., min_length=1)


class CommercialApprovalRevokedEvent(EventEnvelope):
    event_type: Literal["CommercialApprovalRevoked"] = "CommercialApprovalRevoked"
    payload: CommercialApprovalRevokedPayload


class OpportunityStageChangedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_stage: OpportunityStage
    to_stage: OpportunityStage


class OpportunityStageChangedEvent(EventEnvelope):
    event_type: Literal["OpportunityStageChanged"] = "OpportunityStageChanged"
    payload: OpportunityStageChangedPayload


# -------------------------------------------------------------------
# Handoff records (not events; the state the engine holds about handoffs)
# -------------------------------------------------------------------

class HandoffRecord(BaseModel):
    """
    The engine's record of a handoff that has been initiated. Used by the
    eligibility engine to enforce idempotency: a second approval event for
    an opportunity that already has an active handoff should not create a
    second handoff.
    """
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str = Field(..., pattern=r"^OPP-\d+$")
    triggering_approval_event_id: str
    triggering_approved_contract_version: str
    submitted_at: datetime
    current_status: HandoffStatus

    @field_validator("submitted_at")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("submitted_at must include a timezone.")
        return v
