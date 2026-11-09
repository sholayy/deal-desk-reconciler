"""
Tests for the shared configuration loader and dataframe normalizer.

The module's value depends on three properties:

1. Loading the real YAMLs produces a valid Config without errors.
2. Structurally invalid YAML raises ConfigError with an actionable
   message, not a silent fallback or a downstream crash.
3. The transformation is fully data-driven: renaming a source field in
   the YAML changes the output without any Python edit. This is the
   architectural benefit the refactor claims; without a test proving
   it, the claim is unverified.

These tests do not re-verify classifier behavior. The existing
classifier and regression tests continue to cover end-to-end outputs
against the pinned fixtures.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest
import yaml

from config import (
    Config,
    ConfigError,
    load_config,
    normalize_dataframes,
    validate_alignment,
    validate_contract,
    validate_field_mapping,
)


# -------------------------------------------------------------------
# Fixtures: YAML content fragments reused across tests.
# The baseline mapping and contract are structurally valid and
# intentionally minimal (one shared concept, one SF-only concept, one
# MC-only concept, one contract gap). Each test derives a variant from
# these baselines to isolate the invariant it exercises.
# -------------------------------------------------------------------


_BASELINE_MAPPING = {
    "shared_key": {
        "salesforce": "opportunity_id",
        "mission_control": "mc_project_id",
        "mc_prefix": "MC-",
    },
    "timestamps": {
        "salesforce": "last_modified_ts",
        "mission_control": "updated_at",
    },
    "concepts": {
        "customer_name": {
            "salesforce": "customer_name",
            "mission_control": "account",
        },
        "stage": {
            "salesforce": "stage",
            "mission_control": None,
        },
        "delivery_squad": {
            "salesforce": None,
            "mission_control": "delivery_squad",
        },
    },
    "contract_gap_fields": {
        "handoff_flag": {
            "salesforce": "handoff_submitted",
            "mission_control": "handoff_received",
        },
    },
}


_BASELINE_CONTRACT = {
    "persistence_threshold_hours": {
        "field_discrepancy": 1,
        "missing_record": 24,
        "default": 4,
    },
    "fields": {
        "customer_name": {
            "owner": "salesforce",
            "rule": "ownership",
            "reason": "Commercial record is the legal entity source.",
        },
        "stage": {
            "owner": "salesforce",
            "rule": "ownership",
            "reason": "Commercial lifecycle stage.",
            "salesforce_only": True,
        },
        "delivery_squad": {
            "owner": "mission_control",
            "rule": "ownership",
            "reason": "EPM assigns delivery resourcing.",
            "mission_control_only": True,
        },
    },
    "contract_gaps": {
        "handoff_flag": {"reason": "Flag."},
    },
}


def _write_yaml(tmp_path: Path, name: str, content: dict) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(content))
    return path


# -------------------------------------------------------------------
# Happy path: the real project YAMLs load and validate.
# -------------------------------------------------------------------


def test_load_config_from_project_root_succeeds():
    """
    Without arguments, load_config reads the real field_mapping.yaml
    and contract.yaml from the project root and returns a valid Config.
    If this fails, it means the real YAMLs would fail validation under
    the new loader, which is a release blocker.
    """
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert "shared_key" in cfg.mapping
    assert "fields" in cfg.contract
    assert cfg.mapping_source.endswith("field_mapping.yaml")
    assert cfg.contract_source.endswith("contract.yaml")


# -------------------------------------------------------------------
# Field mapping validators.
# -------------------------------------------------------------------


def test_validate_field_mapping_rejects_duplicate_sf_target(tmp_path):
    """
    Two concepts mapping to the same Salesforce source column is a
    silent-corruption bug: the second rename overwrites the first.
    Must raise with the duplicate column named.
    """
    bad = {
        **_BASELINE_MAPPING,
        "concepts": {
            "customer_name": {
                "salesforce": "customer_name",
                "mission_control": "account",
            },
            "fake_concept": {
                "salesforce": "customer_name",  # collides
                "mission_control": "something",
            },
        },
    }
    with pytest.raises(ConfigError, match=r"Duplicate salesforce column 'customer_name'"):
        validate_field_mapping(bad)


def test_validate_field_mapping_rejects_missing_shared_key():
    bad = {k: v for k, v in _BASELINE_MAPPING.items() if k != "shared_key"}
    with pytest.raises(ConfigError, match=r"missing required 'shared_key' section"):
        validate_field_mapping(bad)


# -------------------------------------------------------------------
# Contract validators.
# -------------------------------------------------------------------


def test_validate_contract_rejects_invalid_owner():
    bad = {
        "fields": {
            "customer_name": {
                "owner": "marketing",  # not in valid_owners
                "rule": "ownership",
            },
        },
    }
    with pytest.raises(ConfigError, match=r"invalid owner 'marketing'"):
        validate_contract(bad)


# -------------------------------------------------------------------
# Alignment validators.
# -------------------------------------------------------------------


def test_validate_alignment_rejects_sf_only_concept_with_mc_column():
    """
    A contract.yaml concept marked salesforce_only must have
    mission_control: null in the mapping. Otherwise the generator or
    adapter could try to send the MC column through, producing a
    reconciliation against a field the SOP says MC doesn't own.
    """
    mapping = {
        "shared_key": {"salesforce": "id", "mission_control": "mc_id", "mc_prefix": ""},
        "timestamps": {"salesforce": "ts", "mission_control": "mc_ts"},
        "concepts": {
            "stage": {"salesforce": "stage", "mission_control": "stage_mc"},
        },
    }
    contract = {
        "fields": {
            "stage": {
                "owner": "salesforce",
                "rule": "ownership",
                "salesforce_only": True,
            },
        },
    }
    with pytest.raises(ConfigError, match=r"marked salesforce_only.*mission_control column"):
        validate_alignment(mapping, contract)


# -------------------------------------------------------------------
# Normalization.
# -------------------------------------------------------------------


def test_normalize_dataframes_strips_mc_prefix_and_applies_concept_renames():
    """
    End-to-end normalization check against a small handwritten frame.
    Exercises shared-key prefix stripping, timestamp renaming, concept
    renames, and contract-gap renames in one pass.
    """
    sf_df = pd.DataFrame([{
        "opportunity_id": "OPP-001",
        "customer_name": "Acme Corp",
        "stage": "Closed Won",
        "last_modified_ts": "2026-10-28T10:00:00Z",
        "handoff_submitted": True,
    }])
    mc_df = pd.DataFrame([{
        "mc_project_id": "MC-OPP-001",
        "account": "Acme Corp",
        "delivery_squad": "Squad A",
        "updated_at": "2026-10-28T11:00:00Z",
        "handoff_received": False,
    }])

    sf_out, mc_out = normalize_dataframes(sf_df, mc_df, _BASELINE_MAPPING)

    # MC prefix stripped, MC join column renamed to SF's.
    assert mc_out["opportunity_id"].iloc[0] == "OPP-001"
    assert "mc_project_id" not in mc_out.columns  # renamed

    # Timestamps suffixed consistently.
    assert "last_modified_ts_sf" in sf_out.columns
    assert "last_modified_ts_mc" in mc_out.columns

    # Concept renames applied.
    assert "customer_name_sf" in sf_out.columns
    assert "customer_name_mc" in mc_out.columns
    assert "stage_sf" in sf_out.columns
    assert "delivery_squad_mc" in mc_out.columns

    # Contract gap renames applied.
    assert "handoff_flag_sf" in sf_out.columns
    assert "handoff_flag_mc" in mc_out.columns


# -------------------------------------------------------------------
# The architectural-benefit test. This is the reviewer's required
# change #4: a change to field_mapping.yaml must change normalization
# behavior without any Python edit. If this test ever fails, the
# refactor's claim is false and we have a regression.
# -------------------------------------------------------------------


def test_renaming_source_field_in_yaml_changes_normalization(tmp_path):
    """
    The reviewer's required architectural proof. If `commercial_contact`
    is renamed to `primary_sponsor` in a temporary field_mapping.yaml,
    the normalization layer must honor the new name without any change
    to Python code. A frame whose MC side carries `primary_sponsor`
    must be renamed to `opportunity_owner_mc`, exactly as the original
    `commercial_contact` would have been.
    """
    # Build a temp mapping where opportunity_owner's MC side is the
    # new column name "primary_sponsor".
    custom_mapping = {
        "shared_key": {
            "salesforce": "opportunity_id",
            "mission_control": "mc_project_id",
            "mc_prefix": "MC-",
        },
        "timestamps": {
            "salesforce": "last_modified_ts",
            "mission_control": "updated_at",
        },
        "concepts": {
            "opportunity_owner": {
                "salesforce": "opportunity_owner",
                "mission_control": "primary_sponsor",  # the renamed column
            },
        },
    }
    custom_contract = {
        "fields": {
            "opportunity_owner": {
                "owner": "salesforce",
                "rule": "ownership",
            },
        },
    }

    mapping_file = _write_yaml(tmp_path, "field_mapping.yaml", custom_mapping)
    contract_file = _write_yaml(tmp_path, "contract.yaml", custom_contract)

    cfg = load_config(mapping_path=mapping_file, contract_path=contract_file)

    sf_df = pd.DataFrame([{
        "opportunity_id": "OPP-001",
        "opportunity_owner": "Alice",
    }])
    mc_df = pd.DataFrame([{
        "mc_project_id": "MC-OPP-001",
        "primary_sponsor": "Alice",  # the renamed source column
    }])

    sf_out, mc_out = normalize_dataframes(sf_df, mc_df, cfg.mapping)

    # The new YAML name is honored: primary_sponsor -> opportunity_owner_mc.
    # If normalize_dataframes had a hardcoded reference to commercial_contact,
    # this assertion would fail.
    assert "opportunity_owner_mc" in mc_out.columns
    assert mc_out["opportunity_owner_mc"].iloc[0] == "Alice"
    # And the old hardcoded name is nowhere to be found.
    assert "commercial_contact" not in mc_out.columns
    assert "primary_sponsor" not in mc_out.columns  # it got renamed


# -------------------------------------------------------------------
# Purity.
# -------------------------------------------------------------------


def test_normalize_dataframes_does_not_mutate_inputs():
    """
    Normalization returns new frames. The caller's inputs must be
    unchanged so a failed normalization leaves the caller's state
    intact.
    """
    sf_df = pd.DataFrame([{"opportunity_id": "OPP-001", "customer_name": "Acme"}])
    mc_df = pd.DataFrame([{"mc_project_id": "MC-OPP-001", "account": "Acme"}])
    sf_cols_before = list(sf_df.columns)
    mc_cols_before = list(mc_df.columns)

    normalize_dataframes(sf_df, mc_df, _BASELINE_MAPPING)

    assert list(sf_df.columns) == sf_cols_before
    assert list(mc_df.columns) == mc_cols_before
