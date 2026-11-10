# ADR 0005: Shared YAML loaders for field mapping and contract

**Status:** Accepted
**Date:** 2026-11-10
**Decision owner:** Deal Desk Engineering

## Context

Three callers read the same two YAML files to understand what Salesforce and Mission Control call each SOP concept and what rule governs it: `reconcile.py` (batch loader), `api/main.py` (FastAPI adapter), and `data/generate.py` (synthetic data generator). Before this ADR landed, each of the three implemented its own view of the YAMLs. The batch loader loaded `field_mapping.yaml` and renamed concepts dynamically. The API hardcoded the same rename dicts and a four-field subset of `contract.yaml` inline. The generator hardcoded MC concept names in its record-building dicts.

Two production-shaped bugs shipped from that duplication:

1. The API adapter mapped Mission Control `program_lead` (MC-only delivery role) to `opportunity_owner_mc` (the commercial owner concept). Classification compared the wrong two fields. Spurious owner-drift discrepancies for every API request.
2. The synthetic generator's owned-field drift injection mutated `program_lead` instead of `commercial_contact`. Volume-based metrics work would have computed against a dataset with zero actual owner drift, because the drifted rows were in a field the classifier does not compare.

The reviewer audit traced both back to the same class of bug: one logic, three implementations, no single source of truth. The reviewer sequenced this refactor ahead of the PostgreSQL event store (ADR 0004) because inconsistent business semantics in the persistence layer are harder to undo than inconsistent semantics in a stateless prototype.

## Decision

A new module `config.py` at the project root owns loading, validating, and transforming configuration. Three functions:

- `load_config()` reads `field_mapping.yaml` and `contract.yaml`, runs validation, returns a `Config` dataclass. Called once per application lifetime at startup, not per request.
- `validate_field_mapping`, `validate_contract`, `validate_alignment` check structural integrity and raise `ConfigError` with actionable messages. Four classes of invalid configuration surfaced: duplicate target column mappings, missing shared-key definitions, invalid owner or rule values, and concept misalignment between the two YAMLs including the intentional-single-system vs missing-shared distinction.
- `normalize_dataframes(sf_df, mc_df, mapping)` applies the shared-key, timestamp, concept, and contract-gap renames in one call. Returns new dataframes; does not mutate the inputs.

Each of the three callers now calls these functions rather than carrying its own YAML parsing and rename logic.

## Alternatives considered

### Alternative 1: Keep the duplication, add cross-caller integration tests

Rejected. Integration tests would catch divergence but not prevent it. A fourth caller added in a future phase would still duplicate the logic. The cost of the shared module is one small file plus validators; the cost of each duplicated copy is one more bug surface.

### Alternative 2: Move to a full feature-flag or configuration-management library

Rejected as premature. The project has two YAML files and three callers. A configuration framework introduces dependency surface and runtime behavior that outweigh the benefit at this scale. `config.py` is the smallest thing that removes the duplication.

### Alternative 3: Use Pydantic for config validation

Considered but rejected for this module. Pydantic is the right fit for request/response validation (and is used in `api/schemas.py` and `handoff/schemas.py`). The config files are simpler flat dicts and the validators are a few dozen lines. A Pydantic schema would roughly match the file length of the current validators without adding safety.

## Consequences

### Positive

- Single source of truth. Every caller reads the same validated `Config` at startup. A rename in `field_mapping.yaml` propagates automatically to the generator, the API, and the batch loader without Python edits.
- The reviewer's required architectural-benefit test proves this directly: `tests/test_config.py::test_renaming_source_field_in_yaml_changes_normalization` renames a field in a temporary YAML and asserts normalization honors it. If this test ever fails, the refactor's core claim is false.
- Fail-closed startup. Structurally invalid YAML raises `ConfigError` at module import rather than returning subtly wrong decisions. The API refuses to serve requests against a broken config.
- 1 shared module replaces ~110 lines of duplicated logic across three callers.

### Negative

- Module-level config loading creates an import-time side effect (file read + validation). Tests that import the config module pay this cost once per session. No production impact.
- A test wishing to inject a different mapping must call `load_config()` with an override path. Existing tests do this where needed.

### Operational

- Implementation sequence: Phase A (config module + tests), Phase B (generator), Phase C (API adapter), Phase D (API full contract), Phase E (batch loader). Each phase passed the full suite before the next. Commits in the public history reflect this.
- The reviewer's required equivalence check (same input through API and CSV produces the same classification output) lives in `tests/test_equivalence.py` and runs as part of the default suite.

## References

- `config.py`: the shared module.
- `tests/test_config.py`: validators and the architectural-benefit test.
- `tests/test_equivalence.py`: API and CSV paths produce the same classification for the same input.
- `AUDIT_NOTES.md`: the record of the two bugs this ADR is designed to retire.
