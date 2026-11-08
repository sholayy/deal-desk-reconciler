"""
Pydantic schemas for the API's reconciliation endpoint.

The eligibility endpoint reuses schemas directly from handoff/schemas.py
because the event contract is defined there. The reconciliation endpoint
needs its own request and response shapes because the classifier takes a
joined DataFrame internally while the API caller sends two parallel lists
of records.

Keeping these shapes here (rather than inside handoff/) preserves the
separation documented in ADR 0001: event validation and reconciliation are
two different operations with two different input shapes. Mixing their
schemas into one module would blur that line.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from handoff.schemas import (
    CommercialApprovalApprovedEvent,
    CommercialApprovalRevokedEvent,
    HandoffRecord,
)


# -------------------------------------------------------------------
# Eligibility endpoint schemas
# -------------------------------------------------------------------

class EligibilityContext(BaseModel):
    """
    Context the eligibility engine needs beyond the event itself.

    In Phase 1 the orchestrator sends this in the request body because we
    have no durable store to look it up from. In Phase 2, when Postgres
    lands, the API will look these up by opportunity_id and the caller
    will stop sending them. This split is documented in ADR 0002.

    Every field defaults to an empty list so callers that have no prior
    state (fresh deployment, tests of individual rules) do not need to
    construct context objects they do not use.
    """
    model_config = ConfigDict(extra="forbid")

    existing_handoffs: list[HandoffRecord] = Field(default_factory=list)
    revocation_events: list[CommercialApprovalRevokedEvent] = Field(default_factory=list)
    later_approval_events: list[CommercialApprovalApprovedEvent] = Field(default_factory=list)


class EligibilityRequest(BaseModel):
    """The event being evaluated, plus its evaluation context."""
    model_config = ConfigDict(extra="forbid")

    event: CommercialApprovalApprovedEvent
    context: EligibilityContext = Field(default_factory=EligibilityContext)


class EligibilityResponse(BaseModel):
    """
    The structured decision the engine produced. Mirrors the dataclass
    returned by handoff.eligibility.evaluate_eligibility but as a Pydantic
    model so FastAPI can serialize it and OpenAPI can document it.
    """
    model_config = ConfigDict(extra="forbid")

    eligible: bool
    reason: str
    details: str
    event_id: str
    opportunity_id: str


# -------------------------------------------------------------------
# Reconciliation endpoint schemas
# -------------------------------------------------------------------

class SalesforceRecord(BaseModel):
    """
    A single Salesforce opportunity record as the API receives it. This is
    the shape the API converts into the DataFrame rules.classify_discrepancies
    currently expects. The adapter layer sits inside the endpoint handler
    in main.py and is deliberately kept small so a future refactor (making
    rules.py take native Python records) has one clear place to land.
    """
    model_config = ConfigDict(extra="allow")

    opportunity_id: str = Field(..., pattern=r"^OPP-\d+$")
    sf_record_id: Optional[str] = None
    customer_name: Optional[str] = None
    contract_value_usd: Optional[float] = None
    commercial_approval_status: Optional[str] = None
    opportunity_owner: Optional[str] = None
    stage: Optional[str] = None
    last_modified_ts: Optional[datetime] = None
    handoff_submitted: Optional[bool] = None


class MissionControlRecord(BaseModel):
    """
    A single Mission Control project record. Field names match the mock
    CSV; the adapter in main.py renames them to the shared vocabulary
    before joining.
    """
    model_config = ConfigDict(extra="allow")

    mc_project_id: str = Field(..., pattern=r"^MC-OPP-\d+$")
    account: Optional[str] = None
    engagement_value: Optional[float] = None
    commercial_contact: Optional[str] = None
    delivery_squad: Optional[str] = None
    kickoff_date: Optional[str] = None
    project_status: Optional[str] = None
    program_lead: Optional[str] = None
    updated_at: Optional[datetime] = None
    handoff_received: Optional[bool] = None


class ReconcileRequest(BaseModel):
    """Two lists of records plus the run timestamp to classify against."""
    model_config = ConfigDict(extra="forbid")

    salesforce_records: list[SalesforceRecord]
    mission_control_records: list[MissionControlRecord]
    run_timestamp: datetime = Field(..., description="UTC ISO 8601 with timezone.")

    @field_validator("run_timestamp")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        """Same chronology discipline as the event envelope: no naive timestamps."""
        if v.tzinfo is None:
            raise ValueError(
                "run_timestamp must include a timezone. "
                "Timezone-naive timestamps are rejected at the boundary."
            )
        return v


class Discrepancy(BaseModel):
    """
    One classified disagreement between the two systems. Mirrors the dict
    shape rules.classify_discrepancies returns, as a Pydantic model so
    FastAPI can serialize it cleanly and OpenAPI can document it.
    """
    model_config = ConfigDict(extra="allow")

    opportunity_id: str
    sf_record_id: Optional[str] = None
    concept: Optional[str] = None
    sf_value: Optional[object] = None
    mc_value: Optional[object] = None
    bucket: str
    rule_applied: str
    action: str
    winning_system: Optional[str] = None
    reason: str
    root_cause_category: Optional[str] = None
    likely_source: Optional[str] = None


class ReconcileResponse(BaseModel):
    """The structured discrepancy list the classifier produced."""
    model_config = ConfigDict(extra="forbid")

    discrepancies: list[Discrepancy]
    count: int = Field(..., description="Convenience count matching len(discrepancies).")


# -------------------------------------------------------------------
# Health endpoint
# -------------------------------------------------------------------

class HealthResponse(BaseModel):
    """Simple readiness probe response."""
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    service: Literal["deal-desk-reconciler-api"] = "deal-desk-reconciler-api"
