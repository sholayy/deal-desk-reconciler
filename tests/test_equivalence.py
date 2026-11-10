"""
Equivalence test: same input, same output, regardless of execution path.

The YAML refactor's core architectural claim is that the API, the batch
loader, and the synthetic generator all read from the same validated
configuration and apply the same transformations. If any two paths
diverge for the same input, this test fails and localizes the regression
to whichever path produced the extra or missing discrepancies.

This is the reviewer's required Phase F check (same input through API
and CSV produces the same classification output). It is the test that
would have caught the api/main.py commercial_contact mapping bug on its
first run, had the test existed then.
"""

from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import app
from reconcile import DATA_DIR, load_data
from rules import classify_discrepancies


# Pin the run timestamp so both paths classify against the same "now."
# This matches the simulated_now in contract.yaml and the pinned ANCHOR_TIME
# used in the other tests. A moving clock would make the paths diverge
# for reasons unrelated to configuration.
PINNED_NOW = datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc)


def _batch_discrepancies():
    """
    Run the pinned CSVs through the same code path reconcile.py executes
    on `python3 reconcile.py`. Returns the raw discrepancy dict list from
    the classifier, before any Pydantic serialization.
    """
    data = load_data()
    sf_df = data["sf_df"]
    mc_df = data["mc_df"]
    contract = data["contract"]
    mapping = data["mapping"]

    joined = pd.merge(
        sf_df, mc_df, on="opportunity_id", how="outer", indicator=True,
    )
    joined = joined.rename(columns={"_merge": "presence"})
    joined["presence"] = joined["presence"].map({
        "both": "both",
        "left_only": "salesforce_only",
        "right_only": "mission_control_only",
    })
    return classify_discrepancies(joined, contract, mapping, PINNED_NOW)


def _records_from_csv(path, timestamp_field, bool_fields):
    """
    Load a CSV into a list of dicts the API can accept. Drops NaN values
    so Optional fields are absent rather than NaN, and coerces booleans
    because pandas reads them as object dtype which Pydantic will not
    accept directly.
    """
    df = pd.read_csv(path)
    records = []
    for _, row in df.iterrows():
        rec = {}
        for col, val in row.to_dict().items():
            if pd.isna(val):
                continue
            if col == timestamp_field:
                rec[col] = str(val)
            elif col in bool_fields:
                rec[col] = bool(val)
            else:
                rec[col] = val
        records.append(rec)
    return records


def _api_discrepancies():
    """
    Load the same pinned CSVs and send them through the API /reconcile
    endpoint. Returns the discrepancy list the API serializes back.
    """
    sf_records = _records_from_csv(
        DATA_DIR / "salesforce_export.csv",
        timestamp_field="last_modified_ts",
        bool_fields={"handoff_submitted"},
    )
    mc_records = _records_from_csv(
        DATA_DIR / "mission_control_export.csv",
        timestamp_field="updated_at",
        bool_fields={"handoff_received"},
    )

    client = TestClient(app)
    response = client.post(
        "/reconcile",
        json={
            "salesforce_records": sf_records,
            "mission_control_records": mc_records,
            "run_timestamp": PINNED_NOW.isoformat(),
        },
    )
    assert response.status_code == 200, (
        f"API returned {response.status_code}: {response.text}"
    )
    return response.json()["discrepancies"]


def _equivalence_key(d):
    """
    The identity of a discrepancy for the purpose of comparing paths.
    opportunity_id + concept + bucket + rule_applied uniquely identifies
    a classification decision. Other fields (sf_value, mc_value, reason)
    are consequences of the same decision; comparing them is comparing
    rendering, not semantics.
    """
    return (
        d.get("opportunity_id"),
        d.get("concept"),
        d.get("bucket"),
        d.get("rule_applied"),
    )


def test_api_and_batch_paths_produce_equivalent_classifications():
    """
    Core claim of the shared-config refactor: both paths read the same
    validated configuration, apply the same normalization, and dispatch
    to the same classifier. Same input must therefore produce the same
    classification.

    If this fails, the output diff localizes the regression:
    - Discrepancies only in batch: the API dropped something (likely a
      Pydantic schema missing a field the classifier compares).
    - Discrepancies only in API: the API added something (likely a
      diverging field mapping or a stale copy of contract.yaml).
    """
    batch = _batch_discrepancies()
    api_result = _api_discrepancies()

    batch_keys = sorted(_equivalence_key(d) for d in batch)
    api_keys = sorted(_equivalence_key(d) for d in api_result)

    only_in_batch = set(batch_keys) - set(api_keys)
    only_in_api = set(api_keys) - set(batch_keys)

    assert not only_in_batch and not only_in_api, (
        f"API and batch paths diverged for the same input.\n"
        f"Only in batch: {sorted(only_in_batch)}\n"
        f"Only in API: {sorted(only_in_api)}\n"
        f"Batch count: {len(batch_keys)}. API count: {len(api_keys)}."
    )
