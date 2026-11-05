"""
rules.py — pure business logic for the Deal Desk reconciliation engine.

This module contains the SOP-applying classifier and the incident-identity
helpers. It has no file I/O, no Slack delivery, no dashboard rendering, no
module-level globals. Everything it needs is passed in as arguments.

That separation exists for one practical reason: an orchestrator (n8n, a
FastAPI service, an AWS Lambda, a scheduled job in another language that
shells out to Python, a unit test fixture) can import this module and call
classify_discrepancies directly without pulling in pandas CSV loaders, YAML
file readers, or HTML generators. The reconciliation layer in reconcile.py
is one caller of this module; others will come later.

Public surface:
  classify_discrepancies(joined, contract, mapping, run_timestamp)
      Returns a list of classified discrepancy dicts for the given joined
      DataFrame. Each discrepancy carries a root-cause category.

  root_cause_for(discrepancy)
      Maps a discrepancy dict to its (category, likely_source) tuple. Fixed
      vocabulary across five categories so downstream can group by the kind
      of structural fix each class of incident needs.

  as_utc_datetime(value)
      Parses an ISO 8601 timestamp into a UTC-aware datetime, or None if the
      value is missing, invalid, or lacks a timezone. Invalid or missing
      values are explicitly treated as "not evidence" rather than as zero.

  incident_key(entry)
      Returns a stable (opportunity_id, concept, bucket) identity for an
      audit log entry, with held-to-flagged state transitions normalized
      so a missing record that stabilized and then escalated is treated as
      one incident across its lifecycle rather than a close-and-open pair.

  latest_prior_resolution(entries, observation_timestamp)
      Finds the most recent resolution in `entries` that falls strictly
      between the original observation and the current one. Returns None
      if no such resolution exists. This is the chronology rule that
      prevents the engine from claiming a fix failed when the resolution
      post-dates the observation that supposedly recurred.
"""

from datetime import timezone

import pandas as pd
from dateutil import parser as date_parser


# -------------------------------------------------------------------
# Root-cause mapping
# -------------------------------------------------------------------

def root_cause_for(d):
    """
    Map a classified discrepancy to a root-cause category and a human-readable
    likely source. The category is fixed vocabulary so the weekly rollup can
    group recurring incidents by the kind of structural fix each one needs,
    rather than by which record happened to trip it. Five categories, mapped
    from the five buckets the classifier produces.
    """
    bucket = d["bucket"]
    if bucket == "contract_gap":
        return (
            "policy_ambiguity",
            "SOP does not name an owner for this field. Policy decision required.",
        )
    if bucket == "missing_record":
        reason = (d.get("reason") or "").lower()
        if "missing from salesforce" in reason:
            return (
                "workflow_gap",
                "Delivery engagement underway but Salesforce opportunity record not opened.",
            )
        return (
            "workflow_gap",
            "Salesforce opportunity approved but Mission Control project not yet opened.",
        )
    if bucket == "owned_field":
        system = (d.get("winning_system") or "owning system").replace("_", " ").title()
        return (
            "ownership_or_sync_gap",
            f"Non-owning system differs. {system} is the policy owner; investigate sync or data-entry path.",
        )
    if bucket == "shared_field":
        return (
            "sync_lag_or_process_gap",
            "Both systems updated independently. Recency rule applied. "
            "Repeated occurrence suggests manual drift or a missed sync.",
        )
    if bucket == "below_threshold":
        return (
            "pending_stabilization",
            "Change still within persistence window. Awaiting re-evaluation on next run.",
        )
    return None, None


# -------------------------------------------------------------------
# Classifier
# -------------------------------------------------------------------

def classify_discrepancies(joined, contract, mapping, run_timestamp):
    """
    For each joined row, inspects every mapped concept field and returns
    a list of classified discrepancies.

    Each discrepancy carries bucket, rule_applied, action, winning_system,
    the SOP's reason, and a root-cause category plus a human-readable
    likely source. 'action' is a DECISION, not an executed write. The
    category and source are what the weekly rollup groups by when it
    identifies which recurring problems need structural fixes.

    `run_timestamp` is passed in rather than read from a module global so
    this function can be called by any caller (the reconciliation entry
    point, a test, an orchestrator) without requiring the caller to patch
    module state first.
    """

    discrepancies = []

    # Thresholds come in two forms:
    #   (a) legacy single number: persistence_threshold_hours: 4
    #   (b) per-bucket dict: persistence_threshold_hours:
    #           field_discrepancy: 1
    #           missing_record: 24
    #           default: 4
    # Different buckets have different natural latencies. A field value
    # should propagate between systems in minutes; a brand-new Mission Control
    # project after a Salesforce approval is a human action that can take
    # a day. One threshold for both is too coarse. The dict form lets each
    # bucket carry its own window.
    threshold_config = contract["persistence_threshold_hours"]
    if isinstance(threshold_config, (int, float)):
        field_threshold = threshold_config
        missing_record_threshold = threshold_config
    else:
        default = threshold_config.get("default", 4)
        field_threshold = threshold_config.get("field_discrepancy", default)
        missing_record_threshold = threshold_config.get("missing_record", default)

    for _, row in joined.iterrows():
        opp_id = row["opportunity_id"]

        if row["presence"] != "both":
            missing_from = (
                "mission_control"
                if row["presence"] == "salesforce_only"
                else "salesforce"
            )
            present_in = row["presence"].replace("_only", "")

            # Apply the same persistence threshold to missing records as to
            # field discrepancies. A record created five minutes ago in
            # Salesforce has not had time to open its Mission Control
            # counterpart. Flagging immediately produces noise. Use the
            # existing side's last_modified_ts as the age reference.
            ts_col = (
                "last_modified_ts_sf"
                if row["presence"] == "salesforce_only"
                else "last_modified_ts_mc"
            )
            existing_ts_raw = row.get(ts_col)
            if pd.notna(existing_ts_raw):
                existing_ts = date_parser.parse(str(existing_ts_raw))
                age_hours = (run_timestamp - existing_ts).total_seconds() / 3600
                if age_hours < missing_record_threshold:
                    discrepancies.append({
                        "opportunity_id": opp_id,
                        "sf_record_id": row.get("sf_record_id"),
                        "concept": None,
                        "sf_value": None,
                        "mc_value": None,
                        "bucket": "below_threshold",
                        "rule_applied": "none",
                        "action": "hold",
                        "winning_system": None,
                        "reason": (
                            f"Record present in {present_in} only "
                            f"({age_hours:.1f}h old); below {missing_record_threshold}h "
                            f"persistence threshold. Counterpart in {missing_from} "
                            f"may not have been opened yet."
                        ),
                    })
                    continue

            discrepancies.append({
                "opportunity_id": opp_id,
                "sf_record_id": row.get("sf_record_id"),
                "concept": None,
                "sf_value": None,
                "mc_value": None,
                "bucket": "missing_record",
                "rule_applied": "none",
                "action": "flag",
                "winning_system": None,
                "reason": f"Record present in {present_in} only; missing from {missing_from}.",
            })
            continue

        for concept, field_rule in contract["fields"].items():
            sf_col = f"{concept}_sf"
            mc_col = f"{concept}_mc"

            if field_rule.get("salesforce_only") or field_rule.get("mission_control_only"):
                continue

            sf_val = row.get(sf_col)
            mc_val = row.get(mc_col)

            if pd.isna(sf_val) or pd.isna(mc_val):
                continue
            if sf_val == mc_val:
                continue

            sf_ts = date_parser.parse(row["last_modified_ts_sf"])
            mc_ts = date_parser.parse(row["last_modified_ts_mc"])
            most_recent = max(sf_ts, mc_ts)
            age_hours = (run_timestamp - most_recent).total_seconds() / 3600

            if age_hours < field_threshold:
                discrepancies.append({
                    "opportunity_id": opp_id,
                    "sf_record_id": row.get("sf_record_id"),
                    "concept": concept,
                    "sf_value": sf_val,
                    "mc_value": mc_val,
                    "bucket": "below_threshold",
                    "rule_applied": "none",
                    "action": "hold",
                    "winning_system": None,
                    "reason": f"Most recent change {age_hours:.1f}h ago; below {field_threshold}h persistence threshold.",
                })
                continue

            if field_rule["rule"] == "ownership":
                owner = field_rule["owner"]
                discrepancies.append({
                    "opportunity_id": opp_id,
                    "sf_record_id": row.get("sf_record_id"),
                    "concept": concept,
                    "sf_value": sf_val,
                    "mc_value": mc_val,
                    "bucket": "owned_field",
                    "rule_applied": "ownership",
                    "action": "auto_correct",
                    "winning_system": owner,
                    "reason": field_rule["reason"],
                })

            elif field_rule["rule"] == "recency":
                winning_system = "salesforce" if sf_ts > mc_ts else "mission_control"
                discrepancies.append({
                    "opportunity_id": opp_id,
                    "sf_record_id": row.get("sf_record_id"),
                    "concept": concept,
                    "sf_value": sf_val,
                    "mc_value": mc_val,
                    "bucket": "shared_field",
                    "rule_applied": "recency",
                    "action": "auto_correct",
                    "winning_system": winning_system,
                    "reason": field_rule["reason"],
                })

        for gap_name, gap_fields in contract.get("contract_gaps", {}).items():
            sf_col = f"{gap_name}_sf"
            mc_col = f"{gap_name}_mc"
            sf_val = row.get(sf_col)
            mc_val = row.get(mc_col)
            if pd.isna(sf_val) or pd.isna(mc_val):
                continue
            if sf_val != mc_val:
                discrepancies.append({
                    "opportunity_id": opp_id,
                    "sf_record_id": row.get("sf_record_id"),
                    "concept": gap_name,
                    "sf_value": sf_val,
                    "mc_value": mc_val,
                    "bucket": "contract_gap",
                    "rule_applied": "none",
                    "action": "flag",
                    "winning_system": None,
                    "reason": gap_fields["reason"],
                })

    # Tag every discrepancy with a root-cause category and likely source.
    # Done at the end rather than inline at each append so there is a single
    # place to update the vocabulary and the mappings over time.
    for d in discrepancies:
        d["root_cause_category"], d["likely_source"] = root_cause_for(d)

    return discrepancies


# -------------------------------------------------------------------
# Incident identity and chronology
# -------------------------------------------------------------------

def as_utc_datetime(value):
    """Parse ISO timestamps consistently; invalid or missing values are not evidence."""
    if not value:
        return None
    try:
        parsed = date_parser.parse(str(value))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def incident_key(entry):
    """Use a stable identity across held -> flagged state transitions.

    A missing record held for stabilization is the same underlying issue as a
    missing record later escalated, not a new/closed pair. Other held field
    discrepancies retain their original identity via concept.
    """
    bucket = entry.get("bucket")
    concept = entry.get("concept")
    if bucket == "below_threshold":
        bucket = "missing_record" if concept is None else "field_discrepancy"
    elif bucket in ("owned_field", "shared_field"):
        bucket = "field_discrepancy"
    return (entry.get("opportunity_id"), concept, bucket)


def latest_prior_resolution(entries, observation_timestamp):
    """Only resolutions *before* an observed repeat can prove a fix did not hold.

    A resolution also must post-date the observation it originally resolved.
    This prevents synthetic replay dates and malformed audit entries from
    producing false recurrence warnings.
    """
    observed_at = as_utc_datetime(observation_timestamp)
    if observed_at is None:
        return None
    eligible = []
    for entry in entries:
        resolution = entry.get("resolution")
        if not isinstance(resolution, dict):
            continue
        fixed_at = as_utc_datetime(resolution.get("resolved_at"))
        original_at = as_utc_datetime(entry.get("run_timestamp"))
        if fixed_at and original_at and original_at <= fixed_at < observed_at:
            eligible.append((fixed_at, resolution))
    return max(eligible, key=lambda item: item[0])[1] if eligible else None
