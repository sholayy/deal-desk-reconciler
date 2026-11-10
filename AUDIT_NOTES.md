# Baseline audit — 2026-11-08

Scope: user-uploaded project ZIP, analyzed and modified in an isolated copy. No external Salesforce, Mission Control or Slack calls were made.

## Verified

- Original extracted ZIP: 55 tests passed, 1 snapshot fixture error. `tests/test_regression.py` expects a snapshot in `tests/snapshots/`, but only the root `snapshots/` file was packaged.
- Corrected snapshot placement.
- Fixed API adapter mapping from Mission Control `commercial_contact` to `opportunity_owner_mc`, rather than incorrectly using `program_lead` (a delivery-side role).
- Changed the API's `delivery_squad` contract flag to `mission_control_only`.
- Prevented a duplicate handoff when an existing handoff is `Accepted`; also reject a repeat of a `ReturnedForRework` handoff under the *same* approved contract version. Retain existing behavior permitting a newer approval version to replace a returned handoff.
- Added focused regression tests for both corrections.
- Updated ADR 0004 to model immutable evidence, incident episodes and a rebuildable projection. This is a proposed design, not an implemented database.

## Follow-up fixes — 2026-11-08 evening

Scope: second-pass audit by the primary engineer, applied to the reviewed working copy. All 59 tests pass after each change.

- Fixed the data generator's owned-field drift: `data/generate.py` was mutating `program_lead` (an MC-only concept the classifier does not compare) instead of `commercial_contact` (MC's mirror of SF's `opportunity_owner`). Verified empirically: with `owned_field_drift=0.25` on 200 opportunities, the generator now produces 50 commercial_contact mismatches against the authoritative SF value. Previously: 0. The drift-rate assertion test passed by coincidence because it checks the summary counter, not the resulting dataset.
- Fixed the data generator's baseline: `program_lead` was initialized equal to the commercial owner. Now draws from a disjoint slice of the person-name pool so program leads and commercial owners are different people in the baseline, reflecting that they are different roles.
- Fixed the integration smoke test in `tests/test_data_generator.py`: the mc_renames dict had the same `program_lead -> opportunity_owner_mc` mapping as the API adapter did before the reviewer's correction. Updated to `commercial_contact -> opportunity_owner_mc`.
- Tightened `test_generated_data_runs_through_classifier_without_errors`: now asserts that non-zero `owned_field_drift` produces at least one `opportunity_owner` discrepancy. The previous assertion (`isinstance(discrepancies, list)`) tolerated semantically meaningless output.
- Deleted the dead duplicate at `snapshots/classifier_baseline.json` (project root). The authoritative copy lives at `tests/snapshots/classifier_baseline.json`, which `tests/test_regression.py` reads.

## Structural issue, resolved — 2026-11-10

The YAML duplication across `api/main.py`, `data/generate.py`, and (inline in) `reconcile.py` has been consolidated behind a single validated loader. See ADR 0005. Phases A through F landed in sequence:

- Phase A: `config.py` with `load_config`, validators, and `normalize_dataframes`. 8 new tests.
- Phase B: `data/generate.py` reads native column names from `field_mapping.yaml`.
- Phase C: `api/main.py` adapter delegates renames to `normalize_dataframes`.
- Phase D: `api/main.py` loads the full `contract.yaml` instead of a four-field subset.
- Phase E: `reconcile.load_data` delegates YAML loading and renames to the shared module.
- Phase F: ADR 0005 landed. `tests/test_equivalence.py` proves the API and CSV paths produce the same classification for the same input.

68 tests passing after the refactor. The two bugs that motivated this work (commercial_contact mapping in the API, owned-field drift target in the generator) cannot recur in that shape because the shared loader is now the only place field mappings live.

## Important unaddressed risks

1. Eligibility context is supplied by callers and may be omitted or empty; absence of known history is not verified absence. Do not attach real delivery-creation side effects until history is retrieved from a trusted durable store, with fail-closed authorization.
2. Contract value uses floating point in parts of the API; upgrade to Decimal, define currency/contract authority, and avoid recency-driven financial writebacks.
3. The project does not yet implement PostgreSQL, Alembic migrations, durable delivery-action idempotency, retry leases, or production authorization.
4. Existing CSV and dashboard output is demo evidence, not real operational performance.

## Validation

Run from repository root: `python -m pytest -q`. No credentials or live webhook are needed.
