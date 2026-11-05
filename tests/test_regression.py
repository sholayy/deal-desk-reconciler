"""
Snapshot-based regression test for the classifier.

The eleven unit tests in test_classifier.py cover specific scenarios the
classifier must handle. They do not, by themselves, prove that the full
classifier output for a known set of inputs is unchanged. This file does
that: it runs the classifier against the project's mock SF + MC exports at
a fixed simulated_now and asserts the full structured output matches a
stored baseline.

Any future refactor (extraction, reorganization, performance change) that
alters the classifier's output for these inputs breaks this test. If the
change was intentional, regenerate the baseline:

    python3 -c "
    import json
    from dateutil import parser
    import rules, reconcile
    data = reconcile.load_data()
    joined = reconcile.join_records(
        data['sf_df'], data['mc_df'],
        data['mapping']['shared_key']['salesforce'],
    )
    out = rules.classify_discrepancies(
        joined, data['contract'], data['mapping'],
        parser.parse('2026-10-28T13:30:00Z'),
    )
    with open('tests/snapshots/classifier_baseline.json', 'w') as f:
        json.dump(json.loads(json.dumps(out, default=str)), f, indent=2, sort_keys=True)
    "

Updating the baseline should be a deliberate commit, with a message
explaining why the classifier output changed, so the baseline shift is
reviewable rather than invisible.
"""

import json
from pathlib import Path

import pytest
from dateutil import parser as date_parser

import reconcile
import rules


SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "classifier_baseline.json"
# The timestamp the baseline was captured at. Changing this requires
# regenerating the baseline because threshold decisions depend on it.
BASELINE_RUN_TIMESTAMP = "2026-10-28T13:30:00Z"


@pytest.fixture
def classifier_output():
    """Run the classifier against the project's mock inputs at the baseline timestamp."""
    data = reconcile.load_data()
    joined = reconcile.join_records(
        data["sf_df"],
        data["mc_df"],
        data["mapping"]["shared_key"]["salesforce"],
    )
    run_timestamp = date_parser.parse(BASELINE_RUN_TIMESTAMP)
    return rules.classify_discrepancies(
        joined, data["contract"], data["mapping"], run_timestamp
    )


@pytest.fixture
def baseline():
    """Load the stored baseline snapshot."""
    assert SNAPSHOT_PATH.exists(), (
        f"Baseline snapshot missing at {SNAPSHOT_PATH}. See docstring to regenerate."
    )
    return json.loads(SNAPSHOT_PATH.read_text())


def test_classifier_output_matches_baseline(classifier_output, baseline):
    """
    Behavioral equivalence: the classifier must produce the same structured
    output as the baseline for the mock inputs. If this test fails, the
    classifier's behavior changed. Confirm the change was intentional, then
    regenerate the baseline (see docstring).
    """
    normalized = json.loads(json.dumps(classifier_output, default=str))
    assert normalized == baseline, (
        "Classifier output differs from baseline. "
        f"Got {len(normalized)} discrepancies, expected {len(baseline)}. "
        "See file-level docstring for how to regenerate the baseline if the change was intentional."
    )


def test_classifier_output_discrepancy_count(classifier_output):
    """Quick sanity check that the count matches what the mock data is designed to produce."""
    assert len(classifier_output) == 7, (
        f"Expected 7 discrepancies from mock data at {BASELINE_RUN_TIMESTAMP}, got {len(classifier_output)}. "
        "If the mock data or thresholds changed, update this assertion and regenerate the baseline."
    )


def test_classifier_output_bucket_distribution(classifier_output):
    """
    Bucket distribution is a stronger invariant than overall count. If this
    changes, something about the classifier logic moved items between
    buckets, which is the kind of refactor regression easiest to miss.
    """
    from collections import Counter
    counts = Counter(d["bucket"] for d in classifier_output)
    assert counts == {
        "missing_record": 1,
        "contract_gap": 1,
        "owned_field": 3,
        "shared_field": 2,
    }, f"Bucket distribution changed: {dict(counts)}"
