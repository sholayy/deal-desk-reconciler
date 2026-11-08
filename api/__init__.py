"""
api — FastAPI wrapper around the business logic in rules.py and handoff/.

This package exposes two endpoints documented in ADR 0002:

    POST /events/opportunity/approved
        Receives a Salesforce CommercialApprovalApproved event, validates
        the payload against the schemas in handoff/, runs the eligibility
        rules in handoff/eligibility.py, and returns a structured
        EligibilityDecision.

    POST /reconcile
        Receives lists of normalized Salesforce and Mission Control
        records, joins them, calls rules.classify_discrepancies, and
        returns the structured discrepancy list.

    GET /health
        Operational readiness probe. Returns 200 when the service is up.

The API does not own business logic. Every decision is delegated to the
pure Python modules that already exist. The API's job is schema validation
at the boundary, dispatch to the right function, and response serialization.
If a bug appears in a decision, it is fixed in rules.py or handoff/, not
here. If a bug appears in request handling, it is fixed here.
"""
