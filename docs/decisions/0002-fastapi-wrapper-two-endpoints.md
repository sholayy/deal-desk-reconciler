# ADR 0002: FastAPI wrapper with two endpoints and a health probe

**Status:** Accepted
**Date:** 2026-11-07
**Decision owner:** Deal Desk Engineering

## Context

Phase 1 of the project established `rules.py` as pure business logic and `handoff/` as the eligibility engine for Salesforce approval events. Both are callable from Python but not from any other process: an orchestrator (n8n, Temporal, a Salesforce Flow HTTP callout) cannot reach them directly.

Phase 1 concludes by placing an HTTP interface in front of the pure logic. ADR 0001 established why event validation and reconciliation must be exposed as separate endpoints rather than one polymorphic surface. This ADR documents the specific technology choice and the shape of the endpoints.

## Decision

Use **FastAPI** to expose three HTTP endpoints:

- `GET /health` — operational readiness probe. Returns 200 when the service is up.
- `POST /events/opportunity/approved` — accepts a `CommercialApprovalApprovedEvent` and its evaluation context, runs `handoff.eligibility.evaluate_eligibility`, returns a structured `EligibilityResponse`.
- `POST /reconcile` — accepts parallel lists of normalized Salesforce and Mission Control records plus a run timestamp, joins them via an internal adapter, calls `rules.classify_discrepancies`, returns a structured `ReconcileResponse`.

Request and response bodies are validated by Pydantic. The OpenAPI specification is auto-generated at `/openapi.json` and the Swagger UI at `/docs`.

## Alternatives considered

### Alternative 1: Flask

Rejected because Flask does not natively produce an OpenAPI spec, which we want for free. Adding `flask-smorest` or `flask-openapi3` gets us there but duplicates effort that FastAPI handles inherently.

### Alternative 2: Raw WSGI with a hand-written JSON parser

Rejected because the time spent writing boilerplate request parsing is time not spent on business logic. The whole point of this layer is to be a thin boundary; a framework that minimizes boundary code is the right choice.

### Alternative 3: gRPC with protobuf schemas

Rejected for now. gRPC is a strong choice for internal service-to-service traffic but introduces friction for a project whose audience includes RevOps analysts who would open the Swagger UI in a browser to understand the API. We can add a gRPC gateway later if any consumer requires it; the business logic lives in `rules.py` and `handoff/`, not in the transport layer, so adding another transport is additive.

### Alternative 4: Single `POST /classify` endpoint

Already rejected in ADR 0001. Event validation and reconciliation are different operations with different input shapes and different error semantics. Collapsing them into one endpoint hides that distinction.

### Alternative 5: Serve endpoints from `reconcile.py` directly

Rejected because the API has different lifecycle concerns (HTTP, request validation, response serialization, OpenAPI documentation) from the batch script. Keeping the two separate preserves the Phase 1 architectural principle that `reconcile.py` is one caller of the business logic, not the home of it.

## Consequences

### Positive

- OpenAPI documentation is auto-generated. Consumers can inspect `/docs` to understand the contract without reading source code.
- Pydantic validation at the request boundary means business handlers never receive malformed payloads. Each handler can assume its input is already type-correct.
- Structured error responses: Pydantic validation errors return 422 with field-level context; business-rule rejections (invalid contract value, revoked approval) return 200 with `eligible: false` and a structured reason. The distinction between "the caller sent us bad data" and "we evaluated your event and it fails our rules" is clear at the HTTP level.
- Testable via FastAPI's `TestClient` without running an actual server, so CI tests stay fast.
- The three endpoints map one-to-one to three business responsibilities, making the API surface memorable.

### Negative

- Adds `fastapi`, `uvicorn`, and `httpx` (for TestClient) to `requirements.txt`. All three are widely used and maintained; not a long-term concern.
- The reconciliation endpoint carries an **adapter layer** inside `_build_joined_dataframe` that converts the API's parallel-lists request shape into the joined DataFrame `rules.classify_discrepancies` currently expects. This is the "wart" named in ADR 0001: the real fix is to refactor `rules.py` to accept native Python records. That refactor is intentionally deferred because the adapter is small, localized, and documented, which means the eventual cleanup is a one-commit change.
- Eligibility context (existing handoffs, revocation events, superseding approvals) must currently be sent in the request body because there is no durable store yet. In Phase 2, when Postgres lands, the API will look these up by `opportunity_id` and the request body will shrink. This transitional shape is documented in the schema and will be revised in a future ADR.

### Operational

- Run locally with `uvicorn api.main:app --reload`.
- Swagger UI is at `http://localhost:8000/docs`.
- Health check is at `/health`; suitable for load balancer liveness probes.
- No authentication is configured. This is intentional for Phase 1 (local development only). ADR 0003 will cover authentication choices (HMAC from Salesforce, JWT via an internal issuer, or API keys) and must be in place before any non-local deployment.
- No rate limiting. Same reasoning; ADR 0003 territory.

## References

- `docs/decisions/0001-split-event-validation-from-reconciliation.md`: the earlier decision that this endpoint split implements.
- `events.md`: the event contract the `/events/opportunity/approved` endpoint validates against.
- `handoff/eligibility.py`: the business logic the event endpoint dispatches to.
- `rules.py`: the business logic the reconcile endpoint dispatches to.
- `api/schemas.py`: request/response Pydantic models.
- `tests/test_api.py`: boundary tests exercising each endpoint and its error paths.
