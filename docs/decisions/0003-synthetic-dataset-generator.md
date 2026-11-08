# ADR 0003: Parameterized synthetic dataset generator

**Status:** Accepted
**Date:** 2026-11-08
**Decision owner:** Deal Desk Engineering

## Context

The project ships with a pinned demo dataset of 14 Salesforce rows and 13 Mission Control rows, hand-designed to trigger every classifier scenario exactly once. That is sufficient for correctness demonstration and keeps the regression snapshot small, but it is insufficient for engineering work that depends on volume:

- Metrics at 14 rows are noise. The weekly rollup's direction label, the business scorecard's first-pass acceptance rate, and the engineering scorecard's duplicate-safety counts all need enough observations to show patterns distinguishable from sampling variation.
- Failure-injection tests want realistic load. A dead-letter queue depth threshold only becomes meaningful when the queue can actually accumulate, which requires more input volume than 14 rows can produce.
- Recurring-incident detection only becomes visible across weeks of history. One week of 14 rows cannot exercise the "recurring for three weeks running" path the dashboard surfaces.
- Baseline-vs-intervention simulations for the business case need enough statistical power that a 5-percentage-point change in first-pass acceptance rate between simulation configurations is visible.

The question is how to produce more data without fabricating specific business claims and without introducing non-determinism that would break the regression snapshots.

## Decision

Build a parameterized synthetic dataset generator at `data/generate.py` that produces reproducible Salesforce and Mission Control CSV exports at arbitrary scale.

Three properties are non-negotiable:

1. **Deterministic given a seed.** Same seed in, byte-identical output out. Every random draw goes through `numpy.random.default_rng(seed)`. The existing regression snapshot keeps using the pinned 14-row dataset; generated data is for new work (Phase 3+ metrics, failure-injection tests, Phase 2 integration tests against the event store) and is itself snapshotted when it anchors a test.
2. **Informed distributions.** Contract values are log-normal (public enterprise SaaS benchmarks support this shape). Record ages are gamma-distributed (captures "most recent, long tail older" pattern of an open opportunity population). Each distribution's rationale lives in the function docstring so the choice is defensible under scrutiny and swappable with evidence.
3. **Controlled drift via rate parameters.** The generator takes a `DriftRates` dataclass with named proportions for each drift pattern (`contract_value_drift`, `missing_mc_record`, `contract_gap`, `owned_field_drift`, `handoff_sla_breach`). Tests and demos vary the drift profile without touching the generator's internals.

The generator writes CSVs matching the schema `reconcile.py` already consumes, so backward compatibility is free.

## Alternatives considered

### Alternative 1: Hand-expand the pinned dataset to 500 rows

Rejected because hand-authoring 500 rows is tedious, error-prone, and produces data that encodes the author's assumptions rather than a defensible distribution. A reviewer who asks "why is the median contract value $347,000 in your dataset" needs a better answer than "I typed it in."

### Alternative 2: Use real company names and figures from public sources

Rejected on confidentiality and liability grounds. Every entity named in the dataset would need verification that we are not misrepresenting its actual commercial situation. Even "publicly available" data about private deals carries risk. The generator uses word-pool crosses (Acme Robotics, Zenith Software) which are unambiguously synthetic.

### Alternative 3: Record a snapshot from a real system (anonymized)

Rejected because anonymization at the field level (hashing names, perturbing values) does not scale to statistical attributes. A real snapshot would carry patterns specific to the source organization that would not generalize, and the distribution would be committed to repository history without a documented basis.

### Alternative 4: Use a third-party synthetic data library (SDV, Mimesis, Faker)

Rejected because the generator's requirements are narrow (five drift patterns, six distributions, deterministic) and a focused 400-line module reads more clearly than wiring a general-purpose library with configuration. If the project later needs multi-table joins with referential integrity or statistical anonymization, SDV becomes a reasonable addition at that point. Not today.

## Consequences

### Positive

- Metrics layer work (Phase 3 and Phase 4) has realistic volume to operate on.
- Failure-injection tests can exercise the dead-letter queue and retry logic at load.
- Any reviewer can regenerate the dataset with one command and verify the distribution choices by reading `data/generate.py`.
- Simulation parameters are a public interface: `generate_dataset(n_opportunities=500, drift_rates=DriftRates(contract_value_drift=0.20), random_seed=42)`. The caller is in control; the generator is a function.
- Tests verify determinism and drift rate honoring. Nine tests in `tests/test_data_generator.py` cover the contract.

### Negative

- Two datasets live in the repo: the pinned `data/salesforce_export.csv` for the demo and regression snapshot, and `data/generated/*` (gitignored) for larger-scale work. Mitigated: every reader knows which is which because the directory separates them.
- Synthetic distributions are informed guesses, not fitted to a specific real distribution. A production deployment would want to refit the distributions against its own observed data. The generator is intentionally parameterized to make that refit easy.
- Generation at 10,000+ rows takes a few seconds (pandas is the bottleneck). Acceptable for the sizes this project cares about; would need rework for millions.

### Operational

- CLI: `python3 -m data.generate --n 500 --seed 42 --output data/generated/`
- Generated files are not committed to the repo. `.gitignore` adds `data/generated/`.
- Future ADRs (Phase 2 Postgres work) will document how the generator extends to produce event streams rather than static CSVs. The same distributions and drift logic will be reused.

## References

- `data/generate.py`: the generator itself.
- `tests/test_data_generator.py`: the contract tests.
- OpsLeader 2025 RevOps Benchmarks and the Pavilion Sales Operations Report inform the contract-value distribution shape.
- `docs/decisions/0002-fastapi-wrapper-two-endpoints.md`: the API that will consume generated data in Phase 2 when the event-based path lands.
