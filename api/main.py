"""
FastAPI application exposing the reconciliation engine and the handoff
eligibility engine as HTTP endpoints.

Run locally:
    uvicorn api.main:app --reload
    # Then open http://localhost:8000/docs for the OpenAPI UI.

Run tests:
    python3 -m pytest tests/test_api.py

Design:
    This module is a thin boundary layer. It validates requests via
    Pydantic, dispatches to the pure business logic in rules.py or
    handoff/eligibility.py, and serializes responses. No business decisions
    live here. If a classifier bug appears, it is fixed in rules.py. If a
    schema evolves, it is updated in api/schemas.py or handoff/schemas.py.
"""

import pandas as pd
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from config import load_config, normalize_dataframes
from handoff.eligibility import evaluate_eligibility
from rules import classify_discrepancies as _classify

from api.schemas import (
    Discrepancy,
    EligibilityRequest,
    EligibilityResponse,
    HealthResponse,
    ReconcileRequest,
    ReconcileResponse,
)


# Load and validate configuration at module import. The reviewer asked for
# this pattern so every classification call runs against a specific validated
# configuration rather than reading YAML on each request. If the YAMLs are
# structurally invalid, the app refuses to start rather than returning
# subtly wrong decisions.
_CONFIG = load_config()


app = FastAPI(
    title="Deal Desk Reconciler API",
    description=(
        "HTTP interface to the reconciliation engine and the handoff eligibility "
        "engine. Two endpoints: /events/opportunity/approved validates a Salesforce "
        "approval event, /reconcile compares normalized records from both systems. "
        "See docs/decisions/0001 for why they are split."
    ),
    version="1.0.0",
)


# -------------------------------------------------------------------
# Health
# -------------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Operational readiness probe.",
    tags=["ops"],
)
def health() -> HealthResponse:
    """
    Returns 200 when the service is up. Cheap check for load balancers and
    uptime monitoring. Does not verify downstream systems; a deeper check
    would belong at /ready or /live and would exercise the database and
    external APIs. Phase 2 territory.
    """
    return HealthResponse()


# -------------------------------------------------------------------
# Eligibility
# -------------------------------------------------------------------

@app.post(
    "/events/opportunity/approved",
    response_model=EligibilityResponse,
    summary="Validate a CommercialApprovalApproved event and return the eligibility decision.",
    tags=["events"],
)
def opportunity_approved(request: EligibilityRequest) -> EligibilityResponse:
    """
    Accepts a Salesforce CommercialApprovalApproved event plus the context
    the eligibility engine needs (existing handoffs, revocation events,
    newer approvals). Runs the rules in handoff.eligibility and returns
    the structured decision.

    Pydantic has already validated the event envelope and payload shape
    by the time this handler runs. The handler's only job is dispatch.

    In Phase 1 the context comes from the request body because we have
    no durable store. In Phase 2 the context will come from the database
    and the request will shrink.
    """
    decision = evaluate_eligibility(
        event=request.event,
        existing_handoffs=request.context.existing_handoffs,
        revocation_events=request.context.revocation_events,
        later_approval_events=request.context.later_approval_events,
    )
    return EligibilityResponse(
        eligible=decision.eligible,
        reason=decision.reason.value,
        details=decision.details,
        event_id=decision.event_id,
        opportunity_id=decision.opportunity_id,
    )


# -------------------------------------------------------------------
# Reconciliation
# -------------------------------------------------------------------

def _records_to_dicts(records, timestamp_field: str) -> list[dict]:
    """
    Convert a list of Pydantic records to a list of plain dicts, coercing
    datetime fields back to ISO 8601 strings. The classifier currently
    expects string timestamps (because it parses them with dateutil) so
    the adapter re-serializes rather than making rules.py polymorphic.
    """
    dicts = []
    for record in records:
        d = record.model_dump()
        if timestamp_field in d and d[timestamp_field] is not None:
            ts = d[timestamp_field]
            if hasattr(ts, "isoformat"):
                d[timestamp_field] = ts.isoformat()
        dicts.append(d)
    return dicts


def _build_joined_dataframe(request: ReconcileRequest) -> pd.DataFrame:
    """
    Convert the API's two parallel lists of records into the joined
    DataFrame shape rules.classify_discrepancies currently expects.

    This adapter is explicitly small and lives in the API boundary. All
    the concept renames (SF columns to <concept>_sf, MC columns to
    <concept>_mc, MC prefix stripping) now live in config.normalize_dataframes
    so the API, the batch loader, and the synthetic generator use the same
    transformation. The previous version of this function duplicated those
    renames inline and shipped a mapping bug; the audit trail is in
    AUDIT_NOTES.md.

    Empty inputs still produce a DataFrame with the join column so the
    merge does not raise. Timestamps are coerced back to ISO strings before
    normalization because the classifier parses them with dateutil.
    """
    mapping = _CONFIG.mapping
    sf_ts = mapping["timestamps"]["salesforce"]
    mc_ts = mapping["timestamps"]["mission_control"]
    sf_id = mapping["shared_key"]["salesforce"]
    mc_id = mapping["shared_key"]["mission_control"]

    sf_dicts = _records_to_dicts(request.salesforce_records, sf_ts)
    mc_dicts = _records_to_dicts(request.mission_control_records, mc_ts)

    # Build the frames with the join column always present, even when empty,
    # so the downstream merge does not raise on an empty side.
    sf_df = pd.DataFrame(sf_dicts) if sf_dicts else pd.DataFrame(columns=[sf_id])
    mc_df = pd.DataFrame(mc_dicts) if mc_dicts else pd.DataFrame(columns=[mc_id])

    sf_df, mc_df = normalize_dataframes(sf_df, mc_df, mapping)

    joined = pd.merge(
        sf_df, mc_df, on=sf_id, how="outer", indicator=True,
    )
    joined = joined.rename(columns={"_merge": "presence"})
    joined["presence"] = joined["presence"].map({
        "both": "both",
        "left_only": "salesforce_only",
        "right_only": "mission_control_only",
    })
    return joined


def _reconcile_contract() -> dict:
    """
    Minimal contract.yaml-equivalent for the API's default use. Callers
    can send their own contract in the request later; for Phase 1 we use
    the same rules as contract.yaml so behavior matches the batch engine.
    Keeping this inline avoids coupling the API to the YAML file on disk.
    """
    return {
        "persistence_threshold_hours": {
            "field_discrepancy": 1,
            "missing_record": 24,
            "default": 4,
        },
        "fields": {
            "contract_value": {
                "rule": "recency",
                "reason": "AE may renegotiate post-approval; the more recent value is the current truth.",
            },
            "opportunity_owner": {
                "rule": "ownership",
                "owner": "salesforce",
                "reason": "GTM owner is set in Salesforce and governed by Sales management.",
            },
            "customer_name": {
                "rule": "ownership",
                "owner": "salesforce",
                "reason": "Commercial record is the legal entity source.",
            },
            "delivery_squad": {
                "rule": "ownership",
                "owner": "mission_control",
                "reason": "Delivery owns squad assignments.",
                "mission_control_only": True,
            },
        },
        "contract_gaps": {
            "handoff_flag": {
                "reason": (
                    "Named similarly but mean different things. Salesforce indicates the "
                    "commercial side has pushed the handoff; Mission Control indicates the "
                    "delivery team has acknowledged receipt. The SOP has not decided whether "
                    "these should be mirrored or kept separate. Flag until resolved."
                ),
            },
        },
    }


@app.post(
    "/reconcile",
    response_model=ReconcileResponse,
    summary="Compare Salesforce and Mission Control records; return discrepancies.",
    tags=["reconciliation"],
)
def reconcile(request: ReconcileRequest) -> ReconcileResponse:
    """
    Accepts parallel lists of normalized Salesforce and Mission Control
    records plus a run timestamp. Joins them, applies the SOP, returns
    the structured discrepancy list.

    The caller supplies the run timestamp explicitly rather than inheriting
    it from server wall-clock. This preserves the chronology discipline
    from events.md: the engine's view of "now" is an input, not a hidden
    environmental assumption.
    """
    joined = _build_joined_dataframe(request)
    contract = _reconcile_contract()
    mapping = {}  # The classifier only uses mapping for shared_key, which the joined DF already enforces.
    raw_discrepancies = _classify(joined, contract, mapping, request.run_timestamp)
    return ReconcileResponse(
        discrepancies=[Discrepancy(**d) for d in raw_discrepancies],
        count=len(raw_discrepancies),
    )


# -------------------------------------------------------------------
# Error handler — keep validation errors structured for callers.
# -------------------------------------------------------------------

@app.exception_handler(ValueError)
async def value_error_handler(request, exc: ValueError):
    """
    Convert ValueErrors raised inside handlers (business-level complaints
    the pure logic surfaces) into structured 422 responses. Pydantic
    validation errors from the request boundary are handled automatically
    by FastAPI with the right status code.
    """
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": str(exc), "error_type": "business_rule_violation"},
    )
