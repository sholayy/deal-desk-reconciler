"""
handoff — the business logic for evaluating whether a commercially approved
opportunity is eligible to initiate a delivery handoff.

This package implements the three state machines defined in events.md
(commercial approval, opportunity stage, delivery handoff) and the
eligibility rules that gate the transition from approved commercial terms
to a Submitted handoff.

Design principles:
    - Business logic lives in pure Python functions with no I/O.
    - Pydantic schemas validate payload structure at the boundary.
    - Eligibility decisions are structured, not boolean, so downstream
      consumers (orchestrator, dashboard, audit log) carry the reason.
    - Every rejection reason has a stable identifier so metrics can
      measure the rate of each failure mode independently.
"""
