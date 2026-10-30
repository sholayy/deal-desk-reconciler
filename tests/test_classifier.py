"""
Unit tests for classify_discrepancies.

The classifier is the one place where the SOP is applied, so it's the one
place that most needs regression protection. Each test builds a one-row
joined dataframe directly rather than loading CSVs, which keeps the tests
focused on classifier logic and makes failures easy to read.
"""

import pandas as pd
import pytest

import reconcile


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------

@pytest.fixture
def contract():
    """
    A minimal SOP covering all four bucket types:
      - contract_value → recency (shared field)
      - opportunity_owner → ownership, salesforce wins
      - delivery_squad → ownership, mission_control wins
      - handoff_flag → contract gap (no owner named)
    simulated_now is pinned so threshold math is deterministic.
    """
    return {
        "persistence_threshold_hours": 4,
        "simulated_now": "2026-10-28T13:30:00Z",
        "salesforce_base_url": "https://example.com",
        "fields": {
            "contract_value": {
                "rule": "recency",
                "reason": "Recency rule applies to shared field.",
            },
            "opportunity_owner": {
                "rule": "ownership",
                "owner": "salesforce",
                "reason": "Salesforce owns commercial fields.",
            },
            "delivery_squad": {
                "rule": "ownership",
                "owner": "mission_control",
                "reason": "Mission Control owns delivery fields.",
            },
        },
        "contract_gaps": {
            "handoff_flag": {
                "reason": "SOP has not named an owner for this field.",
            },
        },
    }


@pytest.fixture
def mapping():
    """The classifier reads from the contract and the pre-joined columns, so
    the mapping dict is only passed through. Minimal shape is enough."""
    return {
        "shared_key": {"salesforce": "opportunity_id"},
        "concepts": {},
    }


@pytest.fixture(autouse=True)
def _set_run_timestamp(contract):
    """Pin RUN_TIMESTAMP for every test so threshold math is deterministic."""
    reconcile._set_run_timestamp(contract)


def _row(
    opp_id="OPP-001",
    presence="both",
    sf_ts="2026-10-26T00:00:00Z",
    mc_ts="2026-10-26T00:00:00Z",
    **field_values,
):
    """Build a one-row joined dataframe. Only the columns a test cares about
    need to be set; everything else is left as a missing column, which the
    classifier skips via pd.isna checks."""
    data = {
        "opportunity_id": [opp_id],
        "sf_record_id": ["abc123"],
        "presence": [presence],
        "last_modified_ts_sf": [sf_ts if presence != "mission_control_only" else None],
        "last_modified_ts_mc": [mc_ts if presence != "salesforce_only" else None],
    }
    for key, value in field_values.items():
        data[key] = [value]
    return pd.DataFrame(data)


# -------------------------------------------------------------------
# Ownership rule
# -------------------------------------------------------------------

def test_owned_field_salesforce_wins(contract, mapping):
    joined = _row(
        opportunity_owner_sf="Alice",
        opportunity_owner_mc="Bob",
    )
    result = reconcile.classify_discrepancies(joined, contract, mapping)
    owner_entries = [d for d in result if d["concept"] == "opportunity_owner"]
    assert len(owner_entries) == 1
    d = owner_entries[0]
    assert d["bucket"] == "owned_field"
    assert d["action"] == "auto_correct"
    assert d["winning_system"] == "salesforce"


def test_owned_field_mission_control_wins(contract, mapping):
    joined = _row(
        delivery_squad_sf="Squad A",
        delivery_squad_mc="Squad B",
    )
    result = reconcile.classify_discrepancies(joined, contract, mapping)
    squad_entries = [d for d in result if d["concept"] == "delivery_squad"]
    assert len(squad_entries) == 1
    d = squad_entries[0]
    assert d["bucket"] == "owned_field"
    assert d["action"] == "auto_correct"
    assert d["winning_system"] == "mission_control"


# -------------------------------------------------------------------
# Recency rule
# -------------------------------------------------------------------

def test_recency_salesforce_newer_wins(contract, mapping):
    joined = _row(
        sf_ts="2026-10-27T00:00:00Z",
        mc_ts="2026-10-25T00:00:00Z",
        contract_value_sf=500000,
        contract_value_mc=480000,
    )
    result = reconcile.classify_discrepancies(joined, contract, mapping)
    value_entries = [d for d in result if d["concept"] == "contract_value"]
    assert len(value_entries) == 1
    d = value_entries[0]
    assert d["bucket"] == "shared_field"
    assert d["action"] == "auto_correct"
    assert d["winning_system"] == "salesforce"


def test_recency_mission_control_newer_wins(contract, mapping):
    joined = _row(
        sf_ts="2026-10-25T00:00:00Z",
        mc_ts="2026-10-27T00:00:00Z",
        contract_value_sf=500000,
        contract_value_mc=480000,
    )
    result = reconcile.classify_discrepancies(joined, contract, mapping)
    value_entries = [d for d in result if d["concept"] == "contract_value"]
    assert len(value_entries) == 1
    assert value_entries[0]["winning_system"] == "mission_control"


# -------------------------------------------------------------------
# Persistence threshold
# -------------------------------------------------------------------

def test_below_threshold_field_is_held(contract, mapping):
    """A field disagreement with the most recent change inside the window holds, not corrects."""
    recent = "2026-10-28T12:00:00Z"  # 1.5h before simulated_now
    joined = _row(
        sf_ts=recent,
        mc_ts=recent,
        contract_value_sf=500000,
        contract_value_mc=480000,
    )
    result = reconcile.classify_discrepancies(joined, contract, mapping)
    assert len(result) == 1
    assert result[0]["bucket"] == "below_threshold"
    assert result[0]["action"] == "hold"


# -------------------------------------------------------------------
# Missing records
# -------------------------------------------------------------------

def test_missing_from_mission_control_recent_is_held(contract, mapping):
    """A Salesforce-only record younger than the threshold holds, not flags."""
    recent = "2026-10-28T11:30:00Z"  # 2h before simulated_now
    joined = _row(presence="salesforce_only", sf_ts=recent)
    result = reconcile.classify_discrepancies(joined, contract, mapping)
    assert len(result) == 1
    d = result[0]
    assert d["bucket"] == "below_threshold"
    assert d["action"] == "hold"
    # The held-missing reason uses "present in X only" + "counterpart in Y" rather
    # than the flagged phrasing "missing from Y". Checking the direction indicator.
    reason = d["reason"].lower()
    assert "salesforce only" in reason
    assert "mission_control" in reason


def test_missing_from_mission_control_old_is_flagged(contract, mapping):
    old = "2026-10-21T00:00:00Z"
    joined = _row(presence="salesforce_only", sf_ts=old)
    result = reconcile.classify_discrepancies(joined, contract, mapping)
    assert len(result) == 1
    d = result[0]
    assert d["bucket"] == "missing_record"
    assert d["action"] == "flag"
    assert "missing from mission_control" in d["reason"].lower()


def test_missing_from_salesforce_old_is_flagged(contract, mapping):
    """Reverse direction: Mission Control has the record, Salesforce does not."""
    old = "2026-10-21T00:00:00Z"
    joined = _row(presence="mission_control_only", mc_ts=old)
    result = reconcile.classify_discrepancies(joined, contract, mapping)
    assert len(result) == 1
    d = result[0]
    assert d["bucket"] == "missing_record"
    assert d["action"] == "flag"
    assert "missing from salesforce" in d["reason"].lower()


# -------------------------------------------------------------------
# Contract gap
# -------------------------------------------------------------------

def test_contract_gap_flag(contract, mapping):
    joined = _row(
        handoff_flag_sf="true",
        handoff_flag_mc="false",
    )
    result = reconcile.classify_discrepancies(joined, contract, mapping)
    gap_entries = [d for d in result if d["concept"] == "handoff_flag"]
    assert len(gap_entries) == 1
    d = gap_entries[0]
    assert d["bucket"] == "contract_gap"
    assert d["action"] == "flag"
    assert d["winning_system"] is None


# -------------------------------------------------------------------
# No-change and null handling
# -------------------------------------------------------------------

def test_matching_values_produce_nothing(contract, mapping):
    joined = _row(
        contract_value_sf=500000,
        contract_value_mc=500000,
        opportunity_owner_sf="Alice",
        opportunity_owner_mc="Alice",
    )
    result = reconcile.classify_discrepancies(joined, contract, mapping)
    assert result == []


def test_null_value_is_skipped_silently(contract, mapping):
    """Documented known limitation: NaN on either side bypasses the comparison.
    This test pins that behavior so a future fix is a deliberate change, not an accident."""
    joined = _row(
        contract_value_sf=500000,
        contract_value_mc=None,
    )
    result = reconcile.classify_discrepancies(joined, contract, mapping)
    value_entries = [d for d in result if d["concept"] == "contract_value"]
    assert value_entries == []
