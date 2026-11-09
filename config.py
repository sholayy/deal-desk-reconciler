"""
Shared configuration loader and dataframe normalization.

Single source of truth for `field_mapping.yaml` and `contract.yaml`. The
batch reconciler, the API adapter, and the synthetic data generator all
load from this module so the three paths cannot drift. Before this
module landed, each place hardcoded its own subset of the two YAML
files, which produced the mapping bugs documented in AUDIT_NOTES.

Design decisions worth naming:

- `load_config()` reads both YAML files AND validates them before
  returning. A caller holding a `Config` instance has already passed the
  validation gate. There is no silent fallback; a structurally broken
  YAML raises `ConfigError` with an actionable message, which is the
  reviewer's required behavior.
- Validation is split into three functions so each can be tested in
  isolation and so error messages name the specific file and line of
  concern.
- `normalize_dataframes()` is the reusable transform that takes raw
  Salesforce and Mission Control dataframes plus a loaded mapping and
  returns both frames renamed into the SOP's concept vocabulary. It
  covers shared keys, timestamps, concepts, and contract-gap fields in
  one place. `reconcile.load_data`, `api.main._build_joined_dataframe`
  and `data.generate` all call it so the transformation lives in
  exactly one place.
- Loading is explicit: no caching, no module-level state. Each caller
  (reconcile.py, api.main, data.generate) calls `load_config` once at
  startup and holds the returned `Config` for its lifetime. This is the
  "load at the application boundary" pattern the reviewer asked for and
  it is what lets a future deployment associate every decision with a
  specific validated configuration version.

See `docs/decisions/0005-shared-yaml-loaders.md` for the ADR that
established this pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml


PROJECT_ROOT = Path(__file__).resolve().parent


class ConfigError(ValueError):
    """Raised when field_mapping.yaml or contract.yaml are structurally invalid.

    Inherits ValueError rather than being a bare Exception so existing
    `except ValueError` handlers in the project (the FastAPI 422 handler,
    for example) catch config errors cleanly.
    """


@dataclass(frozen=True)
class Config:
    """A loaded and validated configuration pair.

    Callers should hold one instance per process lifetime. The
    `*_source` attributes record which files were loaded, for
    auditability when Phase 2's event store attaches a
    `ruleset_version` to each classification decision.
    """

    mapping: dict
    contract: dict
    mapping_source: str
    contract_source: str


# -------------------------------------------------------------------
# Loading
# -------------------------------------------------------------------


def load_config(
    mapping_path: Optional[Path] = None,
    contract_path: Optional[Path] = None,
) -> Config:
    """Load, validate, and return the project configuration.

    Defaults to the YAML files in the project root. Overriding the
    paths is for tests and alternate deployments. Raises `ConfigError`
    if either file is structurally invalid or if the two are
    misaligned.
    """
    mapping_path = (
        Path(mapping_path) if mapping_path else PROJECT_ROOT / "field_mapping.yaml"
    )
    contract_path = (
        Path(contract_path) if contract_path else PROJECT_ROOT / "contract.yaml"
    )

    with open(mapping_path, "r") as f:
        mapping = yaml.safe_load(f)
    with open(contract_path, "r") as f:
        contract = yaml.safe_load(f)

    validate_field_mapping(mapping)
    validate_contract(contract)
    validate_alignment(mapping, contract)

    return Config(
        mapping=mapping,
        contract=contract,
        mapping_source=str(mapping_path),
        contract_source=str(contract_path),
    )


# -------------------------------------------------------------------
# Validation
# -------------------------------------------------------------------


def validate_field_mapping(mapping: dict) -> None:
    """Raise `ConfigError` if `field_mapping.yaml` has structural problems.

    Checks:
    - shared_key section present with salesforce and mission_control keys
    - timestamps section present
    - concepts section present
    - No two concepts in the same system share the same source column
      (which would silently drop one of them during a rename)
    """
    if "shared_key" not in mapping:
        raise ConfigError(
            "field_mapping.yaml missing required 'shared_key' section. "
            "It must define salesforce, mission_control, and mc_prefix."
        )
    required_sk = {"salesforce", "mission_control"}
    missing_sk = required_sk - set(mapping["shared_key"].keys())
    if missing_sk:
        raise ConfigError(
            f"shared_key is missing required fields: {sorted(missing_sk)}. "
            "Each defines the join column name on that system."
        )

    if "timestamps" not in mapping:
        raise ConfigError(
            "field_mapping.yaml missing required 'timestamps' section. "
            "It must name the salesforce and mission_control timestamp columns."
        )
    if "concepts" not in mapping:
        raise ConfigError(
            "field_mapping.yaml missing required 'concepts' section."
        )

    # Check for duplicate target column names within each source system.
    # A duplicate would cause the second concept's rename to silently
    # overwrite the first's, dropping one comparison entirely.
    for system_name, system_key in [
        ("salesforce", "salesforce"),
        ("mission_control", "mission_control"),
    ]:
        seen: dict[str, str] = {}
        for concept, cols in mapping["concepts"].items():
            col = cols.get(system_key)
            if col is None:
                continue
            if col in seen:
                raise ConfigError(
                    f"Duplicate {system_name} column '{col}' mapped to both "
                    f"concepts '{seen[col]}' and '{concept}'. Each source "
                    "column must map to at most one concept."
                )
            seen[col] = concept


def validate_contract(contract: dict) -> None:
    """Raise `ConfigError` if `contract.yaml` has invalid ownership values.

    The rules engine assumes owner is one of three specific strings. An
    invalid value would be silently ignored by the classifier, producing
    a field that is in the SOP but not actually governed by any rule.
    """
    valid_owners = {"salesforce", "mission_control", "shared"}
    valid_rules = {"ownership", "recency"}
    for field_name, field_def in contract.get("fields", {}).items():
        owner = field_def.get("owner")
        if owner not in valid_owners:
            raise ConfigError(
                f"Field '{field_name}' in contract.yaml has invalid owner "
                f"'{owner}'. Must be one of {sorted(valid_owners)}."
            )
        rule = field_def.get("rule")
        if rule not in valid_rules:
            raise ConfigError(
                f"Field '{field_name}' in contract.yaml has invalid rule "
                f"'{rule}'. Must be one of {sorted(valid_rules)}."
            )


def validate_alignment(mapping: dict, contract: dict) -> None:
    """Raise `ConfigError` if concepts in contract and mapping do not align.

    Also distinguishes an intentional single-system concept (salesforce_only
    or mission_control_only flag plus a null on the other side in the
    mapping) from a missing column that should have been supplied.
    """
    mapping_concepts = set(mapping.get("concepts", {}).keys())
    contract_concepts = set(contract.get("fields", {}).keys())

    in_mapping_not_contract = mapping_concepts - contract_concepts
    in_contract_not_mapping = contract_concepts - mapping_concepts

    if in_mapping_not_contract:
        raise ConfigError(
            f"Concepts in field_mapping.yaml but not in contract.yaml: "
            f"{sorted(in_mapping_not_contract)}. Every mapped concept must "
            "have a governance rule in the contract."
        )
    if in_contract_not_mapping:
        raise ConfigError(
            f"Concepts in contract.yaml but not in field_mapping.yaml: "
            f"{sorted(in_contract_not_mapping)}. Every ruled concept must "
            "have at least one source column mapped to it."
        )

    # For each aligned concept, confirm that single-system flags in the
    # contract match null columns in the mapping, and vice versa.
    for concept in mapping_concepts & contract_concepts:
        cols = mapping["concepts"][concept]
        rule = contract["fields"][concept]
        sf_present = cols.get("salesforce") is not None
        mc_present = cols.get("mission_control") is not None
        sf_only_flag = rule.get("salesforce_only", False)
        mc_only_flag = rule.get("mission_control_only", False)

        if sf_only_flag and mc_present:
            raise ConfigError(
                f"Concept '{concept}' is marked salesforce_only in "
                "contract.yaml but field_mapping.yaml defines a "
                "mission_control column for it. A salesforce_only concept "
                "must have mission_control: null in the mapping."
            )
        if mc_only_flag and sf_present:
            raise ConfigError(
                f"Concept '{concept}' is marked mission_control_only in "
                "contract.yaml but field_mapping.yaml defines a salesforce "
                "column for it. A mission_control_only concept must have "
                "salesforce: null in the mapping."
            )
        if not sf_only_flag and not mc_only_flag and not (sf_present and mc_present):
            raise ConfigError(
                f"Concept '{concept}' is not flagged as single-system in "
                "contract.yaml but field_mapping.yaml has a null column on "
                "one side. Either add the missing source column or mark the "
                "concept salesforce_only or mission_control_only in the "
                "contract."
            )


# -------------------------------------------------------------------
# Normalization
# -------------------------------------------------------------------


def normalize_dataframes(
    sf_df: pd.DataFrame,
    mc_df: pd.DataFrame,
    mapping: dict,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rename raw source columns into the SOP's concept vocabulary.

    Applies, in order:
    1. Shared key: strip the configured MC prefix from the MC join
       column, then rename MC's join column to Salesforce's name.
    2. Timestamps: suffix SF's timestamp with _sf; rename MC's
       timestamp column to the SF name suffixed with _mc, so both
       frames expose timestamp columns under the same base name.
    3. Concepts: rename each non-null source column to <concept>_sf or
       <concept>_mc.
    4. Contract gap fields: same suffix treatment as concepts.

    Returns new dataframes; does not mutate the inputs. Operates on
    whatever columns are actually present in each frame, so a frame
    missing a particular column is left alone for that column rather
    than raising.
    """
    sf_df = sf_df.copy()
    mc_df = mc_df.copy()

    # Shared key.
    mc_prefix = mapping["shared_key"].get("mc_prefix", "")
    mc_id_col = mapping["shared_key"]["mission_control"]
    sf_id_col = mapping["shared_key"]["salesforce"]
    if mc_id_col in mc_df.columns and mc_prefix:
        mc_df[mc_id_col] = mc_df[mc_id_col].str.replace(
            mc_prefix, "", regex=False,
        )
    if mc_id_col in mc_df.columns and mc_id_col != sf_id_col:
        mc_df = mc_df.rename(columns={mc_id_col: sf_id_col})

    # Timestamps.
    if "timestamps" in mapping:
        sf_ts = mapping["timestamps"].get("salesforce")
        mc_ts = mapping["timestamps"].get("mission_control")
        if sf_ts and sf_ts in sf_df.columns:
            sf_df = sf_df.rename(columns={sf_ts: f"{sf_ts}_sf"})
        if sf_ts and mc_ts and mc_ts in mc_df.columns:
            mc_df = mc_df.rename(columns={mc_ts: f"{sf_ts}_mc"})

    # Concepts.
    sf_renames: dict[str, str] = {}
    mc_renames: dict[str, str] = {}
    for concept, cols in mapping.get("concepts", {}).items():
        sf_col = cols.get("salesforce")
        mc_col = cols.get("mission_control")
        if sf_col and sf_col in sf_df.columns:
            sf_renames[sf_col] = f"{concept}_sf"
        if mc_col and mc_col in mc_df.columns:
            mc_renames[mc_col] = f"{concept}_mc"
    sf_df = sf_df.rename(columns=sf_renames)
    mc_df = mc_df.rename(columns=mc_renames)

    # Contract gap fields: same suffix treatment.
    for gap_name, cols in mapping.get("contract_gap_fields", {}).items():
        sf_col = cols.get("salesforce")
        mc_col = cols.get("mission_control")
        if sf_col and sf_col in sf_df.columns:
            sf_df = sf_df.rename(columns={sf_col: f"{gap_name}_sf"})
        if mc_col and mc_col in mc_df.columns:
            mc_df = mc_df.rename(columns={mc_col: f"{gap_name}_mc"})

    return sf_df, mc_df
