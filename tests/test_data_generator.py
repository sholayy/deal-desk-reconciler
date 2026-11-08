"""
Tests for the synthetic dataset generator.

The generator's value depends on three properties: it is deterministic
given a seed, it honors the requested drift rates within tolerance, and
its output is consumable by the classifier without schema fixes. These
tests pin all three.

Nothing here asserts on specific business behavior (that lives in the
classifier tests). This file verifies the generator's own contract.
"""

from datetime import datetime, timezone

import pandas as pd
import pytest

from data.generate import DriftRates, generate_dataset
from rules import classify_discrepancies


ANCHOR_TIME = datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc)


# -------------------------------------------------------------------
# Determinism
# -------------------------------------------------------------------

def test_same_seed_produces_identical_output():
    """
    Reproducibility is non-negotiable: identical inputs must produce
    byte-equivalent outputs. If this test fails, some source of randomness
    is not going through the seeded generator (likely a stray numpy or
    pandas call, or a Python hash-based iteration).
    """
    a = generate_dataset(n_opportunities=100, random_seed=42, simulated_now=ANCHOR_TIME)
    b = generate_dataset(n_opportunities=100, random_seed=42, simulated_now=ANCHOR_TIME)
    pd.testing.assert_frame_equal(a.sf_df, b.sf_df)
    pd.testing.assert_frame_equal(a.mc_df, b.mc_df)
    assert a.summary == b.summary


def test_different_seeds_produce_different_outputs():
    """
    Sanity: different seeds must produce different data. If they produce
    identical output, the seeding is not actually wired into the random
    stream (a silent bug a careless refactor could introduce).
    """
    a = generate_dataset(n_opportunities=100, random_seed=1, simulated_now=ANCHOR_TIME)
    b = generate_dataset(n_opportunities=100, random_seed=2, simulated_now=ANCHOR_TIME)
    assert not a.sf_df.equals(b.sf_df)


# -------------------------------------------------------------------
# Volume
# -------------------------------------------------------------------

def test_sf_row_count_matches_requested_n():
    result = generate_dataset(n_opportunities=250, random_seed=42)
    assert len(result.sf_df) == 250


def test_mc_row_count_reflects_missing_rate():
    """
    MC row count equals total n minus the number of injected missing
    records. Exact to one, because missing count is `int(n * rate)`.
    """
    result = generate_dataset(
        n_opportunities=1000,
        drift_rates=DriftRates(missing_mc_record=0.05),
        random_seed=42,
    )
    assert len(result.mc_df) == 1000 - int(1000 * 0.05)
    assert result.summary["drift_counts"]["missing_mc_record"] == 50


# -------------------------------------------------------------------
# Schema compatibility
# -------------------------------------------------------------------

_EXPECTED_SF_COLUMNS = {
    "opportunity_id", "customer_name", "contract_value_usd",
    "commercial_approval_status", "opportunity_owner", "stage",
    "last_modified_ts", "handoff_submitted", "sf_record_id",
}

_EXPECTED_MC_COLUMNS = {
    "mc_project_id", "account", "engagement_value", "commercial_contact",
    "delivery_squad", "kickoff_date", "project_status", "program_lead",
    "updated_at", "handoff_received",
}


def test_sf_schema_matches_pinned_contract():
    """Generator output must match the schema the existing reconcile.py
    expects. If we add a column for the pinned data, the generator must
    keep producing it. If we rename, both places move together."""
    result = generate_dataset(n_opportunities=10, random_seed=42)
    assert set(result.sf_df.columns) == _EXPECTED_SF_COLUMNS


def test_mc_schema_matches_pinned_contract():
    result = generate_dataset(n_opportunities=10, random_seed=42)
    assert set(result.mc_df.columns) == _EXPECTED_MC_COLUMNS


# -------------------------------------------------------------------
# Drift rates
# -------------------------------------------------------------------

def test_drift_counts_match_requested_rates_at_scale():
    """
    Drift rates are honored exactly (not approximately) because the
    generator uses `int(n * rate)` to compute the target count. This
    makes tests deterministic. The rounding means 500 * 0.03 = 15 (not
    14.99), and the generator produces exactly 15.
    """
    rates = DriftRates(
        contract_value_drift=0.10,
        missing_mc_record=0.04,
        contract_gap=0.02,
        owned_field_drift=0.08,
    )
    result = generate_dataset(
        n_opportunities=500, drift_rates=rates, random_seed=42,
    )
    counts = result.summary["drift_counts"]
    assert counts["contract_value_drift"] == 50
    assert counts["missing_mc_record"] == 20
    assert counts["contract_gap"] == 10
    assert counts["owned_field_drift"] == 40


def test_zero_drift_rates_produce_clean_output():
    """
    Zero drift rates produce a dataset where SF and MC agree on every
    field for every opportunity. This is the "nothing wrong" baseline
    future metrics tests compare against.
    """
    rates = DriftRates(
        contract_value_drift=0.0,
        missing_mc_record=0.0,
        contract_gap=0.0,
        owned_field_drift=0.0,
        handoff_sla_breach=0.0,
    )
    result = generate_dataset(n_opportunities=200, drift_rates=rates, random_seed=42)
    assert len(result.sf_df) == 200
    assert len(result.mc_df) == 200
    for count in result.summary["drift_counts"].values():
        assert count == 0


# -------------------------------------------------------------------
# Classifier compatibility (integration smoke)
# -------------------------------------------------------------------

def test_generated_data_runs_through_classifier_without_errors():
    """
    Integration smoke: feed the generator output into the actual
    classifier through the same adapter path reconcile.py uses. If this
    fails, the generator's schema has drifted from what the classifier
    expects even though the column-set tests above pass. Catches subtler
    issues like dtype mismatches.
    """
    result = generate_dataset(n_opportunities=100, random_seed=42)

    # Mirror the field-mapping step from reconcile.load_data.
    sf_df = result.sf_df.copy()
    mc_df = result.mc_df.copy()
    mc_df["opportunity_id"] = mc_df["mc_project_id"].str.replace("MC-", "", regex=False)

    sf_renames = {
        "contract_value_usd": "contract_value_sf",
        "opportunity_owner": "opportunity_owner_sf",
        "customer_name": "customer_name_sf",
        "last_modified_ts": "last_modified_ts_sf",
        "handoff_submitted": "handoff_flag_sf",
    }
    mc_renames = {
        "engagement_value": "contract_value_mc",
        "program_lead": "opportunity_owner_mc",
        "account": "customer_name_mc",
        "delivery_squad": "delivery_squad_mc",
        "updated_at": "last_modified_ts_mc",
        "handoff_received": "handoff_flag_mc",
    }
    sf_df = sf_df.rename(columns=sf_renames)
    mc_df = mc_df.rename(columns=mc_renames).drop(columns=["mc_project_id"])

    joined = pd.merge(sf_df, mc_df, on="opportunity_id", how="outer", indicator=True)
    joined = joined.rename(columns={"_merge": "presence"})
    joined["presence"] = joined["presence"].map({
        "both": "both",
        "left_only": "salesforce_only",
        "right_only": "mission_control_only",
    })

    contract = {
        "persistence_threshold_hours": {
            "field_discrepancy": 1,
            "missing_record": 24,
            "default": 4,
        },
        "fields": {
            "contract_value": {"rule": "recency", "reason": "Recency rule."},
            "opportunity_owner": {
                "rule": "ownership", "owner": "salesforce",
                "reason": "SF owns.",
            },
            "customer_name": {
                "rule": "ownership", "owner": "salesforce",
                "reason": "SF owns.",
            },
        },
        "contract_gaps": {"handoff_flag": {"reason": "Gap."}},
    }
    mapping = {}
    discrepancies = classify_discrepancies(joined, contract, mapping, ANCHOR_TIME)
    # We do not assert specific counts; we assert the classifier returns
    # a list without raising. If drift was injected at the requested rates,
    # discrepancies should be present but the exact count depends on
    # chronology interactions we do not want to pin here.
    assert isinstance(discrepancies, list)
