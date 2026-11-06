# ADR 0001: Split event validation from reconciliation

**Status:** Accepted
**Date:** 2026-11-05
**Decision owner:** Deal Desk Engineering

## Context

The existing reconciliation engine (`rules.classify_discrepancies`) takes a joined representation of both Salesforce and Mission Control records and returns classified discrepancies between them. Its input shape is a pandas DataFrame carrying columns from both systems side by side.

As we extend the project toward event-driven processing, a natural question is whether to route live Salesforce events (as defined in `events.md`) through the same classifier endpoint. A single `POST /classify` endpoint accepting any input shape would appear simpler and might reduce the number of surfaces the caller has to understand.

In practice this would conflate two operations that have different inputs, different outputs, and different business meaning:

- **Reconciliation** compares two systems and names their disagreements. Its input is the state of both systems. Its output is a list of discrepancies, each tied to a specific field pair.
- **Event validation** accepts a single system's state change and decides whether it is a well-formed, business-valid trigger for downstream work. Its input is one event. Its output is a single eligibility decision.

These are not variations of the same operation. They have different authentication boundaries, different error semantics, different retry profiles, and different audit requirements.

## Decision

Expose them as two separate HTTP endpoints, backed by two separate Python modules:

- `POST /events/opportunity` — receives a Salesforce event (defined in `events.md`), validates its envelope and payload against the appropriate Pydantic schemas, runs the eligibility rules in `handoff.eligibility`, and returns a structured `EligibilityDecision`.
- `POST /reconcile` — receives a joined representation of Salesforce and Mission Control records, calls `rules.classify_discrepancies`, and returns the structured discrepancy list.

The two endpoints share the same FastAPI application but no shared business logic. Each has its own request/response schema, its own test suite, and its own failure modes.

## Alternatives considered

### Alternative 1: single `POST /classify` endpoint accepting both shapes

Reject either shape with a 400 if the other is expected. Dispatch internally based on the presence of a `joined` field versus an `event` field.

Rejected because:
- Validation logic becomes a dispatcher, not a validator. Pydantic cannot cleanly validate a union of unrelated shapes without sacrificing the per-field error specificity that makes 400 responses actionable.
- Audit log entries lose the ability to carry a stable `endpoint` identifier, which weakens the metrics we want to produce (percentage of events rejected vs percentage of reconciliations run, broken down separately).
- The API surface becomes harder to document. OpenAPI consumers see one endpoint with a polymorphic input, which obscures the two distinct operations the business actually performs.

### Alternative 2: route events through the classifier by fabricating Mission Control data

Accept a Salesforce event, fabricate a placeholder Mission Control record, and call the existing classifier.

Rejected because:
- Fabricating one side of a reconciliation is dishonest. The classifier's output would carry decisions based on placeholder data that never existed in the source system.
- It silently redefines what the classifier does. A hiring reviewer or a future engineer reading `rules.py` would reasonably assume both inputs come from real systems.
- The failure mode is invisible: the classifier would return plausible-looking discrepancies that do not correspond to any real system state.

### Alternative 3: redefine the classifier to accept a single system's state

Rewrite `rules.classify_discrepancies` to accept any single-system event and compare it against a locally held projection of the other system's state.

Rejected because:
- The projection is itself a reconciliation problem. We would be hiding the two-system comparison inside the classifier rather than separating it.
- It couples the classifier to a specific storage model (how is the projection stored? when is it refreshed? what happens on projection staleness?) that does not belong in the business logic layer.
- It violates the Phase 1 architectural principle established in rules.py: pure business logic with no I/O and no module-level state.

## Consequences

### Positive

- Each endpoint is independently testable. The 35 tests we have today split cleanly across the two modules without overlap.
- Pydantic can enforce precise schemas for each shape. Validation errors carry per-field context.
- The audit log carries a stable `endpoint` identifier, so metrics can measure event ingestion and reconciliation separately.
- Future orchestration work (an n8n workflow calling `/events/opportunity`, a scheduled job calling `/reconcile`) can route to the right endpoint without inspecting payload shape.
- The documentation surface is clean: two endpoints, two responsibilities, two sets of error semantics.

### Negative

- Two Python modules to maintain instead of one. Mitigated: the eligibility engine and the classifier share almost no business logic; the only common piece is the chronology helpers in `rules.py`, which both modules import.
- Two API paths for callers to learn. Mitigated: the OpenAPI spec documents both; the role of each is clear from the path name.
- Slight duplication of envelope parsing (Pydantic model instantiation happens at both endpoints). Accepted: this is cheap and explicit.

### Operational

- The deployment unit stays the same (one FastAPI application, one container).
- The monitoring surface grows: separate dashboards for event validation volume and reconciliation volume.
- The incident response runbook grows: a 5xx from `/events/opportunity` and a 5xx from `/reconcile` have different investigation paths.

## References

- `events.md`: event contract definition that drives `/events/opportunity`.
- `rules.py`: pure business logic the reconciler calls.
- `handoff/eligibility.py`: eligibility rules the event validator calls.
- Reviewer feedback (external, 2026-11-05) that identified this conflation as the first architectural problem to resolve.
