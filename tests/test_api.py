"""
Tests for the FastAPI boundary.

The test strategy mirrors the design split documented in ADR 0002: the
business logic is already covered by test_classifier.py, test_recurrence.py,
test_regression.py, and test_handoff_eligibility.py. This file only exercises
the boundary itself:

    - Does the service start cleanly?
    - Does each endpoint accept its documented request shape?
    - Does Pydantic reject malformed input with 422?
    - Does the right pure business function get called with the right args?
    - Does the response serialize cleanly into the documented schema?

If a test here needs to assert on business logic correctness (which category
a specific discrepancy receives, which rule rejects a specific event), that
assertion belongs in the existing unit or regression tests, not here. The
API handler is not where business bugs hide once the handlers are a thin
dispatch layer.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app


BASE_TIME = datetime(2026, 11, 5, 14, 23, 17, tzinfo=timezone.utc)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# -------------------------------------------------------------------
# Health
# -------------------------------------------------------------------

def test_health_returns_ok(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "service": "deal-desk-reconciler-api"}


# -------------------------------------------------------------------
# Eligibility
# -------------------------------------------------------------------

def _valid_approval_payload(
    opportunity_id: str = "OPP-012",
    event_id: str = "evt_001",
    approved_contract_version: str = "v3",
    contract_value_usd: float = 480000,
    opportunity_owner_id: str = "USR-029",
) -> dict:
    """Build a dict in the shape the API expects. Lives here (not in a fixture
    module) so each test can diff from the baseline easily."""
    return {
        "event": {
            "event_id": event_id,
            "event_type": "CommercialApprovalApproved",
            "event_version": "1.0.0",
            "emitted_at": BASE_TIME.isoformat(),
            "source": {
                "system": "salesforce",
                "org_id": "00D6g000003ABCDEAG",
                "actor": None,
            },
            "opportunity_id": opportunity_id,
            "payload": {
                "approved_contract_version": approved_contract_version,
                "contract_value_usd": contract_value_usd,
                "currency": "USD",
                "approval_notes": "Standard terms.",
                "opportunity_owner_id": opportunity_owner_id,
                "delivery_scope_summary": "12-week implementation.",
            },
        },
        "context": {
            "existing_handoffs": [],
            "revocation_events": [],
            "later_approval_events": [],
        },
    }


def test_eligible_approval_returns_eligible(client: TestClient):
    response = client.post(
        "/events/opportunity/approved",
        json=_valid_approval_payload(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is True
    assert body["reason"] == "eligible"
    assert body["opportunity_id"] == "OPP-012"
    assert body["event_id"] == "evt_001"


def test_malformed_event_missing_required_field_returns_422(client: TestClient):
    """A payload missing a required Pydantic field should be rejected at the
    request boundary with 422, not reach the handler."""
    payload = _valid_approval_payload()
    del payload["event"]["payload"]["contract_value_usd"]
    response = client.post("/events/opportunity/approved", json=payload)
    assert response.status_code == 422


def test_timezone_naive_emitted_at_returns_422(client: TestClient):
    """The chronology discipline from events.md requires timezone info;
    a naive timestamp must not pass the request boundary."""
    payload = _valid_approval_payload()
    payload["event"]["emitted_at"] = "2026-11-05T14:23:17"  # No tzinfo
    response = client.post("/events/opportunity/approved", json=payload)
    assert response.status_code == 422


def test_invalid_contract_value_returns_eligible_false(client: TestClient):
    """
    Business-rule rejections (zero contract value, revoked approval, etc.)
    come back as 200 with eligible=False and a structured reason. They are
    valid API responses, not HTTP errors. Only payload validation errors
    produce 4xx responses.
    """
    payload = _valid_approval_payload(contract_value_usd=-100.0)
    response = client.post("/events/opportunity/approved", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is False
    assert body["reason"] == "invalid_contract_value"


def test_revoked_approval_returns_eligible_false(client: TestClient):
    """Chronology rule: a revocation that post-dates the approval and names
    the same contract version makes the approval ineligible."""
    payload = _valid_approval_payload()
    payload["context"]["revocation_events"] = [
        {
            "event_id": "evt_revoke",
            "event_type": "CommercialApprovalRevoked",
            "event_version": "1.0.0",
            "emitted_at": (BASE_TIME + timedelta(hours=2)).isoformat(),
            "source": {
                "system": "salesforce",
                "org_id": "00D6g000003ABCDEAG",
                "actor": None,
            },
            "opportunity_id": "OPP-012",
            "payload": {
                "previously_approved_contract_version": "v3",
                "revocation_reason": "Terms renegotiated.",
            },
        }
    ]
    response = client.post("/events/opportunity/approved", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is False
    assert body["reason"] == "approval_revoked"


# -------------------------------------------------------------------
# Reconciliation
# -------------------------------------------------------------------

def _reconcile_payload(
    sf_records: list[dict] | None = None,
    mc_records: list[dict] | None = None,
    run_timestamp: datetime = BASE_TIME,
) -> dict:
    return {
        "salesforce_records": sf_records or [],
        "mission_control_records": mc_records or [],
        "run_timestamp": run_timestamp.isoformat(),
    }


def test_reconcile_empty_lists_returns_no_discrepancies(client: TestClient):
    response = client.post("/reconcile", json=_reconcile_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["discrepancies"] == []


def test_reconcile_matching_records_returns_no_discrepancies(client: TestClient):
    """One SF record and one MC record with identical shared-field values
    and no contract gaps should produce zero discrepancies."""
    sf = {
        "opportunity_id": "OPP-100",
        "sf_record_id": "0066g000TEST100",
        "customer_name": "Acme Robotics",
        "contract_value_usd": 100000.0,
        "opportunity_owner": "Alice",
        "last_modified_ts": "2026-10-20T00:00:00Z",
    }
    mc = {
        "mc_project_id": "MC-OPP-100",
        "account": "Acme Robotics",
        "engagement_value": 100000.0,
        "commercial_contact": "Alice",
        "updated_at": "2026-10-20T00:00:00Z",
    }
    response = client.post(
        "/reconcile",
        json=_reconcile_payload(sf_records=[sf], mc_records=[mc]),
    )
    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_reconcile_owned_field_mismatch_returns_discrepancy(client: TestClient):
    """SF and MC disagree on the opportunity owner (SF-owned concept).
    The classifier should propose a correction with Salesforce winning."""
    sf = {
        "opportunity_id": "OPP-101",
        "sf_record_id": "0066g000TEST101",
        "customer_name": "Zenith Software",
        "contract_value_usd": 250000.0,
        "opportunity_owner": "Priya Shah",
        "last_modified_ts": "2026-10-20T00:00:00Z",
    }
    mc = {
        "mc_project_id": "MC-OPP-101",
        "account": "Zenith Software",
        "engagement_value": 250000.0,
        "commercial_contact": "Jordan Kim",  # Mismatch with SF
        "updated_at": "2026-10-20T00:00:00Z",
    }
    response = client.post(
        "/reconcile",
        json=_reconcile_payload(sf_records=[sf], mc_records=[mc]),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    discrepancy = body["discrepancies"][0]
    assert discrepancy["opportunity_id"] == "OPP-101"
    assert discrepancy["concept"] == "opportunity_owner"
    assert discrepancy["bucket"] == "owned_field"
    assert discrepancy["winning_system"] == "salesforce"
    assert discrepancy["root_cause_category"] == "ownership_or_sync_gap"


def test_reconcile_timezone_naive_timestamp_returns_422(client: TestClient):
    """The run_timestamp also enforces the chronology discipline."""
    payload = _reconcile_payload()
    payload["run_timestamp"] = "2026-11-05T14:23:17"  # No tzinfo
    response = client.post("/reconcile", json=payload)
    assert response.status_code == 422


def test_reconcile_missing_sf_record_returns_flag(client: TestClient):
    """
    MC has a record but SF does not. This should surface as a missing_record
    flag in the "missing from salesforce" direction. The classifier uses
    the MC record's updated_at to decide whether to hold or flag; we use
    an old timestamp so it flags.
    """
    mc = {
        "mc_project_id": "MC-OPP-200",
        "account": "Pacific Logistics",
        "engagement_value": 75000.0,
        "commercial_contact": "Bob",
        "updated_at": "2026-09-01T00:00:00Z",
    }
    response = client.post(
        "/reconcile",
        json=_reconcile_payload(sf_records=[], mc_records=[mc]),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    discrepancy = body["discrepancies"][0]
    assert discrepancy["opportunity_id"] == "OPP-200"
    assert discrepancy["bucket"] == "missing_record"
    assert discrepancy["action"] == "flag"
    assert "missing from salesforce" in discrepancy["reason"].lower()


# -------------------------------------------------------------------
# OpenAPI schema
# -------------------------------------------------------------------

def test_openapi_schema_lists_three_endpoints(client: TestClient):
    """The generated OpenAPI spec should document health, events, and
    reconcile. If someone adds an endpoint and does not update the schemas,
    this test catches undocumented API surface."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/health" in paths
    assert "/events/opportunity/approved" in paths
    assert "/reconcile" in paths


def test_reconcile_program_lead_is_not_commercial_owner(client: TestClient):
    """Delivery's program lead is not the Salesforce commercial owner."""
    sf = {"opportunity_id": "OPP-301", "opportunity_owner": "Priya Shah",
          "last_modified_ts": "2026-10-20T00:00:00Z"}
    mc = {"mc_project_id": "MC-OPP-301", "program_lead": "Jordan Kim",
          "commercial_contact": "Priya Shah", "updated_at": "2026-10-20T00:00:00Z"}
    response = client.post("/reconcile", json=_reconcile_payload(sf_records=[sf], mc_records=[mc]))
    assert response.status_code == 200
    assert not any(x["concept"] == "opportunity_owner" for x in response.json()["discrepancies"])
