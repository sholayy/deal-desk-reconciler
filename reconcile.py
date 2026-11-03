"""
Deal Desk Reconciliation

Reads daily exports from Salesforce and Mission Control, applies the SOP
(contract.yaml), and produces three outputs:
  - A daily Slack message (output/slack/)
  - An HTML dashboard (output/dashboard.html)
  - A JSON observation history (output/reconciliation_log.json); resolve.py adds metadata

The engine proposes corrections based on the SOP; it does not currently
write back to either source system. See README for the full roadmap.

Run: python reconcile.py
"""

import json
import os
import uuid
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import pandas as pd
import requests
import yaml
from dateutil import parser as date_parser

# -------------------------------------------------------------------
# Paths — all file locations are defined once here so they can be
# changed in one place if the project structure moves.
# -------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
SLACK_DIR = OUTPUT_DIR / "slack"

SALESFORCE_CSV = DATA_DIR / "salesforce_export.csv"
MISSION_CONTROL_CSV = DATA_DIR / "mission_control_export.csv"
CONTRACT_YAML = PROJECT_ROOT / "contract.yaml"
FIELD_MAPPING_YAML = PROJECT_ROOT / "field_mapping.yaml"

AUDIT_LOG = OUTPUT_DIR / "reconciliation_log.json"
DASHBOARD_HTML = OUTPUT_DIR / "dashboard.html"

# Ensure output directories exist
OUTPUT_DIR.mkdir(exist_ok=True)
SLACK_DIR.mkdir(exist_ok=True)

# -------------------------------------------------------------------
# Run metadata — timestamp, date, and a unique run_id so repeat
# executions don't collide in the audit log or inflate the dashboard.
# -------------------------------------------------------------------

RUN_TIMESTAMP = None
RUN_DATE_STR = None
RUN_ID = None


def _set_run_timestamp(contract, override=None):
    """Sets the global run timestamp and a unique run ID.

    Precedence: an explicit `override` ISO string (from the CLI `--now`)
    wins over `simulated_now` in contract.yaml, which in turn wins over
    wall-clock UTC. The override exists so a demo of the full detect-fix-recur
    loop can be driven from three commands without editing contract.yaml
    between runs.
    """
    global RUN_TIMESTAMP, RUN_DATE_STR, RUN_ID
    source = override or contract.get("simulated_now")
    if source:
        RUN_TIMESTAMP = date_parser.parse(source)
    else:
        RUN_TIMESTAMP = datetime.now(timezone.utc)
    RUN_DATE_STR = RUN_TIMESTAMP.strftime("%Y-%m-%d")
    RUN_ID = str(uuid.uuid4())


def _set_salesforce_base_url(contract):
    """Sets the module-level Salesforce base URL from config."""
    global _SF_BASE_URL
    _SF_BASE_URL = contract.get("salesforce_base_url")


# -------------------------------------------------------------------
# Load — reads inputs and returns everything the engine needs.
# -------------------------------------------------------------------

def load_data():
    """
    Reads the two CSV exports and the two YAML configs.

    Applies the field mapping so Mission Control columns are renamed to
    the SOP's concept names. From this point onwards, both dataframes
    speak the same vocabulary.
    """

    with open(CONTRACT_YAML, "r") as f:
        contract = yaml.safe_load(f)

    with open(FIELD_MAPPING_YAML, "r") as f:
        mapping = yaml.safe_load(f)

    sf_df = pd.read_csv(SALESFORCE_CSV)
    mc_df = pd.read_csv(MISSION_CONTROL_CSV)

    mc_prefix = mapping["shared_key"]["mc_prefix"]
    mc_id_col = mapping["shared_key"]["mission_control"]
    mc_df[mc_id_col] = mc_df[mc_id_col].str.replace(mc_prefix, "", regex=False)

    sf_id_col = mapping["shared_key"]["salesforce"]
    mc_df = mc_df.rename(columns={mc_id_col: sf_id_col})

    sf_ts_col = mapping["timestamps"]["salesforce"]
    mc_ts_col = mapping["timestamps"]["mission_control"]
    mc_df = mc_df.rename(columns={mc_ts_col: f"{sf_ts_col}_mc"})
    sf_df = sf_df.rename(columns={sf_ts_col: f"{sf_ts_col}_sf"})

    concept_rename_sf = {}
    concept_rename_mc = {}

    for concept, cols in mapping["concepts"].items():
        if cols["salesforce"] is not None:
            concept_rename_sf[cols["salesforce"]] = f"{concept}_sf"
        if cols["mission_control"] is not None:
            concept_rename_mc[cols["mission_control"]] = f"{concept}_mc"

    sf_df = sf_df.rename(columns=concept_rename_sf)
    mc_df = mc_df.rename(columns=concept_rename_mc)

    for gap_name, cols in mapping.get("contract_gap_fields", {}).items():
        if cols["salesforce"] is not None:
            sf_df = sf_df.rename(columns={cols["salesforce"]: f"{gap_name}_sf"})
        if cols["mission_control"] is not None:
            mc_df = mc_df.rename(columns={cols["mission_control"]: f"{gap_name}_mc"})

    return {
        "sf_df": sf_df,
        "mc_df": mc_df,
        "contract": contract,
        "mapping": mapping,
    }


# -------------------------------------------------------------------
# Join — merges the two dataframes on opportunity_id so every opportunity
# has both its Salesforce and Mission Control values side by side.
# -------------------------------------------------------------------

def join_records(sf_df, mc_df, shared_key_col):
    """
    Outer-joins the two dataframes on the shared ID column.

    Outer join is deliberate: records present in only one system must
    survive the join so the classification step can flag them.
    """

    joined = pd.merge(
        sf_df, mc_df, on=shared_key_col, how="outer", indicator=True,
    )

    joined = joined.rename(columns={"_merge": "presence"})
    joined["presence"] = joined["presence"].map({
        "both": "both",
        "left_only": "salesforce_only",
        "right_only": "mission_control_only",
    })

    return joined


# -------------------------------------------------------------------
# Classify — walks every joined row and classifies every disagreement
# into one of the four buckets from the SOP.
#
# IMPORTANT: this step produces decisions, not writes. The 'auto_correct'
# action means the SOP authorizes correction; it does NOT mean Salesforce
# or Mission Control has been updated. The write-back layer is the next
# engineering step (see README).
# -------------------------------------------------------------------

def _root_cause_for(d):
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


def classify_discrepancies(joined, contract, mapping):
    """
    For each joined row, inspects every mapped concept field and returns
    a list of classified discrepancies.

    Each discrepancy carries bucket, rule_applied, action, winning_system,
    the SOP's reason, and a root-cause category plus a human-readable
    likely source. 'action' is a DECISION, not an executed write. The
    category and source are what the weekly rollup groups by when it
    identifies which recurring problems need structural fixes.
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
                age_hours = (RUN_TIMESTAMP - existing_ts).total_seconds() / 3600
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
            age_hours = (RUN_TIMESTAMP - most_recent).total_seconds() / 3600

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
        d["root_cause_category"], d["likely_source"] = _root_cause_for(d)

    return discrepancies


# -------------------------------------------------------------------
# Emit — appends observations to the audit log and writes the Slack message.
# Language is "proposed" throughout, not "applied" — see classifier docstring.
# -------------------------------------------------------------------

def emit_outputs(discrepancies):
    log_entries = []
    for d in discrepancies:
        log_entries.append({
            "run_id": RUN_ID,
            "run_timestamp": RUN_TIMESTAMP.isoformat(),
            "run_date": RUN_DATE_STR,
            "opportunity_id": d["opportunity_id"],
            "concept": d["concept"],
            "sf_value": _serialize(d["sf_value"]),
            "mc_value": _serialize(d["mc_value"]),
            "bucket": d["bucket"],
            "rule_applied": d["rule_applied"],
            "action": d["action"],
            "winning_system": d["winning_system"],
            "reason": d["reason"],
            "root_cause_category": d.get("root_cause_category"),
            "likely_source": d.get("likely_source"),
        })

    existing_log = []
    if AUDIT_LOG.exists():
        with open(AUDIT_LOG, "r") as f:
            existing_log = json.load(f)
    existing_log.extend(log_entries)
    with open(AUDIT_LOG, "w") as f:
        json.dump(existing_log, f, indent=2)

    auto_corrections = [d for d in discrepancies if d["action"] == "auto_correct"]
    flags = [d for d in discrepancies if d["action"] == "flag"]
    held = [d for d in discrepancies if d["action"] == "hold"]

    date_header = RUN_TIMESTAMP.strftime("%A %b %-d")
    lines = [f"*Deal Desk reconciliation — {date_header}*", ""]

    if not flags:
        total_compared = _count_opportunities_in_current_run(existing_log)
        lines.append(
            f"{total_compared} opportunities compared. "
            f"{len(auto_corrections)} corrections proposed by rule. "
            f"Nothing needs attention."
        )
    else:
        unique_flagged_opps = len({d["opportunity_id"] for d in flags})
        lines.append(
            f"{unique_flagged_opps} {_pluralize('opportunity', 'opportunities', unique_flagged_opps)} need attention "
            f"({len(flags)} flagged discrepancies)."
        )
        lines.append("")
        for flag in flags:
            opp_link = _build_opp_link(flag["opportunity_id"], flag.get("sf_record_id"))
            if flag["bucket"] == "missing_record":
                missing_from_sf = "missing from salesforce" in (flag.get("reason") or "").lower()
                missing_direction = "Salesforce" if missing_from_sf else "Mission Control"
                lines.append(
                    f"*{opp_link}* — Missing {missing_direction} record. "
                    f"{flag['reason']} Runbook: Missing record."
                )
            elif flag["bucket"] == "contract_gap":
                lines.append(
                    f"*{opp_link}* — {flag['concept']} fields disagree between systems. "
                    f"SOP does not name an owner. Runbook: Contract gap."
                )
            lines.append("")
        if auto_corrections:
            lines.append(f"{len(auto_corrections)} corrections proposed by rule.")

    if held:
        lines.append(f"({len(held)} held below persistence threshold.)")

    lines.append("")
    lines.append(f"Weekly rollup: output/slack/weekly_{RUN_DATE_STR}.txt")

    slack_path = SLACK_DIR / f"slack_{RUN_DATE_STR}.txt"
    message_body = "\n".join(lines)
    with open(slack_path, "w") as f:
        f.write(message_body)

    slack_posted = _post_to_slack(message_body)

    return {
        "log_entries_written": len(log_entries),
        "slack_message_path": str(slack_path),
        "slack_posted": slack_posted,
        "flags": len(flags),
        "auto_corrections": len(auto_corrections),
        "held": len(held),
    }


# -------------------------------------------------------------------
# Weekly rollup — organizes by incident lifecycle: new, recurring, not observed.
#
# An "incident" is a unique (opportunity_id, concept, bucket) triple. One
# incident can produce many audit log entries if it fires on multiple days
# of the week. Counting observations instead of incidents inflates the
# number and hides the fact that most of the noise is the same unresolved
# issue firing repeatedly, which is a design problem rather than a monitoring
# problem.
#
# The rollup compares this week to the prior week along two dimensions:
# which incidents are new, which are chronic (recurring in both weeks),
# and which were not observed between weeks. Direction (improving / stable / worsening)
# is the headline signal.
# -------------------------------------------------------------------

def _as_utc_datetime(value):
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


def _incident_key(entry):
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


def _latest_prior_resolution(entries, observation_timestamp):
    """Only resolutions *before* an observed repeat can prove a fix did not hold.

    A resolution also must post-date the observation it originally resolved.
    This prevents synthetic replay dates and malformed audit entries from
    producing false recurrence warnings.
    """
    observed_at = _as_utc_datetime(observation_timestamp)
    if observed_at is None:
        return None
    eligible = []
    for entry in entries:
        resolution = entry.get("resolution")
        if not isinstance(resolution, dict):
            continue
        fixed_at = _as_utc_datetime(resolution.get("resolved_at"))
        original_at = _as_utc_datetime(entry.get("run_timestamp"))
        if fixed_at and original_at and original_at <= fixed_at < observed_at:
            eligible.append((fixed_at, resolution))
    return max(eligible, key=lambda item: item[0])[1] if eligible else None


def _incident_label(opp_id, concept, bucket):
    """Human-readable label for an incident triple. Used in the rollup."""
    bucket_names = {
        "missing_record": "missing record",
        "owned_field": "owned field",
        "shared_field": "shared field",
        "below_threshold": "held below threshold",
        "contract_gap": "contract gap",
    }
    bucket_label = bucket_names.get(bucket, bucket)
    if concept:
        return f"{opp_id} {concept} ({bucket_label})"
    return f"{opp_id} ({bucket_label})"


def emit_weekly_rollup():
    if not AUDIT_LOG.exists():
        return None
    with open(AUDIT_LOG, "r") as f:
        log = json.load(f)

    from datetime import timedelta
    from collections import defaultdict

    # Two windows: last 7 days (this week) and the 7 days before that (prior week).
    this_week_start = RUN_TIMESTAMP - timedelta(days=7)
    prior_week_start = RUN_TIMESTAMP - timedelta(days=14)
    this_week_end = RUN_TIMESTAMP

    this_week_raw = [
        e for e in log
        if this_week_start <= date_parser.parse(e["run_timestamp"]) <= this_week_end
    ]
    prior_week_raw = [
        e for e in log
        if prior_week_start <= date_parser.parse(e["run_timestamp"]) < this_week_start
    ]

    if not this_week_raw:
        return None

    def _dedupe_to_latest_run_per_day(entries):
        """Within a window, keep only entries from the latest run of each day."""
        by_day = defaultdict(list)
        for e in entries:
            by_day[e["run_date"]].append(e)
        latest_run_per_day = {}
        for day, day_entries in by_day.items():
            latest = day_entries[-1]  # append order breaks ties for simulated timestamps
            latest_run_per_day[day] = latest.get("run_id")
        return [
            e for e in entries
            if e.get("run_id") == latest_run_per_day.get(e["run_date"])
        ]

    this_week = _dedupe_to_latest_run_per_day(this_week_raw)
    prior_week = _dedupe_to_latest_run_per_day(prior_week_raw)

    def _unique_incidents(entries):
        """Group entries by (opportunity_id, concept, bucket). Each key is one
        incident; its value is the list of audit log entries that observed it."""
        incidents = defaultdict(list)
        for e in entries:
            key = _incident_key(e)
            incidents[key].append(e)
        return incidents

    this_week_incidents = _unique_incidents(this_week)
    prior_week_incidents = _unique_incidents(prior_week)

    this_keys = set(this_week_incidents.keys())
    prior_keys = set(prior_week_incidents.keys())
    recurring_keys = this_keys & prior_keys
    new_keys = this_keys - prior_keys
    closed_keys = prior_keys - this_keys

    week_header = RUN_TIMESTAMP.strftime("Week ending %A %b %-d")
    lines = [f"*Deal Desk reconciliation — weekly rollup: {week_header}*", ""]

    this_unique = len(this_keys)
    this_observations = len(this_week)
    prior_unique = len(prior_keys)
    prior_observations = len(prior_week)

    lines.append(
        f"{this_unique} unique incidents this week ({this_observations} observations)."
    )
    if prior_week:
        if this_unique < prior_unique:
            direction = "improving"
        elif this_unique > prior_unique:
            direction = "worsening"
        else:
            direction = "stable"
        lines.append(
            f"Changes since last week: {len(closed_keys)} not observed this week, "
            f"{len(recurring_keys)} recurring, {len(new_keys)} new. "
            f"Direction: {direction}."
        )
        lines.append(
            "_Prevention check: not observed this week does not prove permanent resolution. "
            "Verified prevention requires a documented fix and subsequent monitoring._"
        )
    else:
        lines.append(
            "No prior-week data yet. The direction and prevention indicators become "
            "meaningful once the engine has run for two weeks."
        )
    lines.append("")

    # Recurring: appeared in both weeks. These are the design problems the
    # system is telling you about. Not whack-a-mole, root cause. Each one
    # carries its root-cause category so the reader can group by the kind
    # of structural fix needed, not just by which record tripped.
    #
    # If a prior-week entry for the same incident has a resolution record,
    # that is a stated fix that did not hold. Call it out explicitly, because
    # a resolution that did not prevent recurrence is more actionable than
    # absence of an observation alone does NOT prove the resolution worked.
    if recurring_keys:
        lines.append("*Recurring incidents — root cause required:*")
        recurring_without_prior_fix = []
        recurring_with_failed_prior_fix = []
        for key in sorted(recurring_keys):
            current_observations = this_week_incidents[key]
            # A prior-week resolution counts as a failed fix only if a fresh
            # observation occurred AFTER the documented resolution.
            prior_resolution = None
            for obs in current_observations:
                candidate = _latest_prior_resolution(
                    prior_week_incidents[key], obs.get("run_timestamp")
                )
                if candidate and (
                    prior_resolution is None
                    or _as_utc_datetime(candidate["resolved_at"])
                    > _as_utc_datetime(prior_resolution["resolved_at"])
                ):
                    prior_resolution = candidate
            if prior_resolution:
                recurring_with_failed_prior_fix.append((key, prior_resolution))
            else:
                recurring_without_prior_fix.append(key)

        def _first_detected_date(opp_id, concept, bucket):
            """Earliest run_date across the full log matching this incident identity,
            using the same normalized key the rest of the rollup uses. Lets a reader
            see how long a problem has existed. An incident that has been firing
            for six weeks reads differently from one first observed last week."""
            target_key = (opp_id, concept, bucket)
            matching = [e for e in log if _incident_key(e) == target_key]
            if not matching:
                return None
            earliest = min(matching, key=lambda e: e.get("run_timestamp", ""))
            return earliest.get("run_date")

        for key in recurring_without_prior_fix:
            opp_id, concept, bucket = key
            this_count = len(this_week_incidents[key])
            prior_count = len(prior_week_incidents[key])
            category = this_week_incidents[key][0].get("root_cause_category") or "uncategorized"
            first_detected = _first_detected_date(opp_id, concept, bucket)
            first_detected_str = f"first detected {first_detected}" if first_detected else "first detected date unknown"
            lines.append(
                f"  • {_incident_label(opp_id, concept, bucket)} "
                f"[possible: {category}]: "
                f"{this_count} observations this week, {prior_count} last week · "
                f"{first_detected_str} · "
                f"root cause not yet captured"
            )

        for key, prior_resolution in recurring_with_failed_prior_fix:
            opp_id, concept, bucket = key
            this_count = len(this_week_incidents[key])
            prior_count = len(prior_week_incidents[key])
            category = this_week_incidents[key][0].get("root_cause_category") or "uncategorized"
            first_detected = _first_detected_date(opp_id, concept, bucket)
            first_detected_str = f"first detected {first_detected}" if first_detected else "first detected date unknown"
            confirmed_cause = prior_resolution.get("root_cause") or "unspecified"
            lines.append(
                f"  • {_incident_label(opp_id, concept, bucket)} "
                f"[possible: {category}]: "
                f"{this_count} observations this week, {prior_count} last week · "
                f"{first_detected_str} · "
                f"STILL DETECTED AFTER RECORDED FIX · "
                f"confirmed cause: \"{confirmed_cause}\", "
                f"action taken: \"{prior_resolution.get('corrective_action')}\" "
                f"(by {prior_resolution.get('responsible_team', 'unknown team')})"
            )
        lines.append("")

        # Group recurring incidents by category so a reviewer sees which
        # category of fix would retire the most incidents at once.
        from collections import Counter
        category_counts = Counter(
            (this_week_incidents[k][0].get("root_cause_category") or "uncategorized")
            for k in recurring_keys
        )
        if len(category_counts) >= 1:
            lines.append("_Recurring by root-cause category:_")
            for category, count in category_counts.most_common():
                lines.append(
                    f"  • {category}: {count} recurring {'incident' if count == 1 else 'incidents'}"
                )
            lines.append("")

        if recurring_with_failed_prior_fix:
            lines.append(
                f"_{len(recurring_with_failed_prior_fix)} recurring {'incident is' if len(recurring_with_failed_prior_fix) == 1 else 'incidents are'} "
                f"still detected after a recorded resolution. "
                f"This is a signal to investigate, not a conclusion that the fix failed. "
                f"Confirm deployment and look for new triggering events before drawing conclusions._"
            )
            lines.append("")

    # New this week: present now, absent last week.
    if new_keys:
        lines.append("*New this week:*")
        for key in sorted(new_keys):
            opp_id, concept, bucket = key
            count = len(this_week_incidents[key])
            label = _incident_label(opp_id, concept, bucket)
            if count > 1:
                lines.append(f"  • {label}: {count} observations")
            else:
                lines.append(f"  • {label}")
        lines.append("")

    # Absent this week: a useful observation, not proof that a root cause was fixed.
    if closed_keys:
        lines.append("*Not observed this week (verify before closing):*")
        for key in sorted(closed_keys):
            opp_id, concept, bucket = key
            lines.append(f"  • {_incident_label(opp_id, concept, bucket)}")
        lines.append("")

    lines.append("Full log: output/reconciliation_log.json")
    lines.append("Runbook: runbook.md")

    weekly_path = SLACK_DIR / f"weekly_{RUN_DATE_STR}.txt"
    weekly_body = "\n".join(lines)
    with open(weekly_path, "w") as f:
        f.write(weekly_body)

    slack_posted = _post_to_slack(weekly_body)

    return {
        "weekly_message_path": str(weekly_path),
        "slack_posted": slack_posted,
        "unique_incidents_this_week": this_unique,
        "observations_this_week": this_observations,
        "unique_incidents_prior_week": prior_unique,
        "recurring_count": len(recurring_keys),
        "new_count": len(new_keys),
        "not_observed_count": len(closed_keys),
        "closed_count": len(closed_keys),  # backward-compatible name
    }


# -------------------------------------------------------------------
# Dashboard — command-center style, single HTML file. Reads only the
# current run's entries. All data-derived content is HTML-escaped.
# -------------------------------------------------------------------

def _h(value):
    """HTML-escape any value for safe inclusion in generated markup."""
    if value is None or (hasattr(value, "__len__") and len(str(value).strip()) == 0):
        return "—"
    if pd.isna(value) if not isinstance(value, (list, dict)) else False:
        return "—"
    return escape(str(value), quote=True)


def _format_concept_name(concept):
    """snake_case -> Title Case for display. 'contract_value' -> 'Contract Value'."""
    if concept is None:
        return "—"
    return str(concept).replace("_", " ").title()


# Fields that should display as currency. Add to this set as the SOP grows.
_CURRENCY_FIELDS = {"contract_value"}


def _format_value_for_display(concept, value):
    """Format a value for display in the dashboard. Money fields get $ and commas."""
    if value is None:
        return "—"
    if pd.isna(value) if not isinstance(value, (list, dict)) else False:
        return "—"
    if concept in _CURRENCY_FIELDS:
        try:
            # Numeric cast handles both "500000.0" strings and raw floats.
            amount = float(value)
            return f"${amount:,.0f}"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _variance_line(concept, current, proposed):
    """
    For monetary fields, show the size of the proposed correction so a
    reviewer sees whether it is material. Returns markup for a single
    muted line below the before/after block, e.g. 'Decrease $20,000 (-4%)'.
    Returns empty string for non-currency fields or unparseable values.
    """
    if concept not in _CURRENCY_FIELDS:
        return ""
    try:
        before = float(current)
        after = float(proposed)
    except (TypeError, ValueError):
        return ""
    delta = after - before
    if delta == 0:
        return ""
    direction = "Increase" if delta > 0 else "Decrease"
    abs_delta = f"${abs(delta):,.0f}"
    percent = ""
    if before:
        pct = delta / before * 100
        # Drop the decimal when it rounds cleanly, keep one otherwise.
        pct_str = f"{pct:+.0f}" if abs(pct - round(pct)) < 0.05 else f"{pct:+.1f}"
        percent = f" ({pct_str}%)"
    return f'<p class="variance">{direction} {abs_delta}{percent}</p>'


def _root_cause_pill(category):
    """Small pill rendered on dashboard cards showing the root-cause category.
    Returns empty markup for None (so held items with no category stay clean)."""
    if not category:
        return ""
    labels = {
        "policy_ambiguity": "Policy ambiguity",
        "workflow_gap": "Workflow gap",
        "upstream_validation_gap": "Upstream validation gap",
        "ownership_or_sync_gap": "Ownership or sync gap",
        "sync_lag_or_process_gap": "Sync lag or process gap",
        "pending_stabilization": "Pending stabilization",
    }
    label = labels.get(category, category.replace("_", " ").title())
    return f'<span class="pill pill-rootcause">{escape(label, quote=True)}</span>'


def _root_cause_line(category, likely_source):
    """
    Render the root-cause pill alongside a one-sentence likely source. The
    pill carries the heuristic category the engine applied; the sentence
    describes what the engine thinks is happening. Both are hypotheses, not
    findings. "Likely cause:" prefix makes that explicit so a reader never
    mistakes the engine's heuristic for a confirmed investigation result.
    """
    if not category:
        return ""
    pill = _root_cause_pill(category)
    source = escape(likely_source or "", quote=True)
    return (
        f'<div class="card-rootcause">{pill}'
        f'<span class="rootcause-text"><span class="rootcause-label">Likely cause:</span> {source}</span>'
        f'</div>'
    )


def _held_eligibility_line(reason, run_timestamp, threshold_hours=4):
    """
    Reads 'Most recent change X.Yh ago; below ...' and returns a human-readable
    eligibility line: 'Eligible for action at HH:MM UTC (in Xh Ym).'
    Falls back to the raw reason if parsing fails.
    """
    import re
    from datetime import timedelta
    match = re.search(r"([\d.]+)h (?:ago|old)", reason or "")
    if not match:
        return reason
    try:
        age_hours = float(match.group(1))
        remaining = threshold_hours - age_hours
        eligible_at = run_timestamp + timedelta(hours=remaining)
        hours_remaining = int(remaining)
        minutes_remaining = int((remaining - hours_remaining) * 60)
        if hours_remaining > 0:
            countdown = f"in {hours_remaining}h {minutes_remaining}m"
        else:
            countdown = f"in {minutes_remaining}m"
        return f"Eligible for action at {eligible_at.strftime('%H:%M')} UTC ({countdown})."
    except (ValueError, TypeError):
        return reason


def emit_dashboard():
    """
    Writes a single HTML dashboard combining:
      - Hero band with four KPIs (opps in scope, needing review, decisions, held)
      - Needs attention queue (flag cards with Salesforce links)
      - Decisions proposed (correction cards with before/after values)
      - Held while they stabilize (brief panel)
      - Decision policy sidebar
      - Raw system state (collapsed)

    No JavaScript, no CDNs, no hidden tabs. All content is visible on scroll.
    """

    sf_df = pd.read_csv(SALESFORCE_CSV)
    mc_df = pd.read_csv(MISSION_CONTROL_CSV)

    with open(AUDIT_LOG, "r") as f:
        log = json.load(f)

    # Filter to only this run so repeat executions don't inflate counts.
    todays_entries = [e for e in log if e.get("run_id") == RUN_ID]

    # Build a map of prior resolutions, keyed by (opp_id, concept, bucket).
    # When an incident recurs after a documented fix, the dashboard flag card
    # surfaces the prior resolution so the viewer sees "fix did not hold"
    # rather than treating the recurrence as a first occurrence.
    prior_resolutions = {}
    for entry in log:
        if entry.get("run_id") == RUN_ID:
            continue
        key = _incident_key(entry)
        prior_resolutions.setdefault(key, []).append(entry)

    opp_customer = dict(zip(sf_df["opportunity_id"], sf_df["customer_name"]))
    opp_sf_record = dict(zip(sf_df["opportunity_id"], sf_df["sf_record_id"]))

    flags = [e for e in todays_entries if e["action"] == "flag"]
    decisions = [e for e in todays_entries if e["action"] == "auto_correct"]
    held = [e for e in todays_entries if e["action"] == "hold"]

    # Denominator: unique opportunities across both systems. Lets the reader
    # see the quiet 90%, not just the exceptions.
    all_sf_ids = set(sf_df["opportunity_id"].dropna().astype(str))
    all_mc_ids = set(mc_df["mc_project_id"].dropna().astype(str).str.replace("MC-", "", regex=False))
    opps_in_scope = len(all_sf_ids | all_mc_ids)

    unique_flagged_opps = len({e["opportunity_id"] for e in flags})

    # Read the stabilization windows from the SOP so a change in contract.yaml
    # flows through to every surface (dashboard header, policy sidebar,
    # held-item caption, eligibility countdown) without touching the engine.
    # The threshold can be a single int (legacy) or a per-bucket dict.
    with open(CONTRACT_YAML, "r") as f:
        threshold_config = yaml.safe_load(f)["persistence_threshold_hours"]
    if isinstance(threshold_config, (int, float)):
        field_threshold_hrs = threshold_config
        record_threshold_hrs = threshold_config
        threshold_summary = f"{threshold_config} hours"
    else:
        default = threshold_config.get("default", 4)
        field_threshold_hrs = threshold_config.get("field_discrepancy", default)
        record_threshold_hrs = threshold_config.get("missing_record", default)
        if field_threshold_hrs == record_threshold_hrs:
            threshold_summary = f"{field_threshold_hrs} hours"
        else:
            threshold_summary = f"{field_threshold_hrs}h fields / {record_threshold_hrs}h records"

    # ---------- Flag cards ----------
    def _render_flag_card(f):
        customer = opp_customer.get(f["opportunity_id"], "")
        sf_record = opp_sf_record.get(f["opportunity_id"], "")
        link_html = ""
        if _SF_BASE_URL and sf_record and not pd.isna(sf_record):
            url = f"{_SF_BASE_URL}/{sf_record}/view"
            link_html = f'<a href="{_h(url)}" class="card-btn" target="_blank" rel="noopener noreferrer">Open in Salesforce ↗</a>'
        if f["bucket"] == "missing_record":
            missing_from_sf = "missing from salesforce" in (f.get("reason") or "").lower()
            if missing_from_sf:
                title = "Missing from Salesforce"
                action = "Investigate why the delivery record has no corresponding Salesforce opportunity."
            else:
                title = "Missing from Mission Control"
                action = "Confirm the engagement is still active, then open the delivery record."
        else:
            title = f"Policy gap: {_h(_format_concept_name(f['concept']))}"
            action = "SOP has not named an owner for this field. Decide and update contract.yaml."
        root_cause_html = _root_cause_line(f.get("root_cause_category"), f.get("likely_source"))

        # If this exact (opp, concept, bucket) was resolved in a prior run,
        # the fix did not hold. Surface it explicitly.
        prior_resolution = _latest_prior_resolution(
            prior_resolutions.get(_incident_key(f), []),
            f.get("run_timestamp"),
        )
        prior_resolution_html = ""
        if prior_resolution:
            resolved_date = (prior_resolution.get("resolved_at") or "")[:10]
            team = prior_resolution.get("responsible_team") or "unknown team"
            confirmed_cause = prior_resolution.get("root_cause") or ""
            fix = prior_resolution.get("corrective_action") or ""
            prior_resolution_html = (
                f'<div class="prior-resolution">'
                f'<strong>Still detected after recorded resolution.</strong> '
                f'Resolved on {_h(resolved_date)} by {_h(team)}.<br>'
                f'<span class="prior-label">Confirmed cause:</span> {_h(confirmed_cause)}<br>'
                f'<span class="prior-label">Action taken:</span> <em>&ldquo;{_h(fix)}&rdquo;</em><br>'
                f'<span class="prior-note">Does not prove the fix failed. Confirm the fix was deployed and investigate for a new triggering event.</span>'
                f'</div>'
            )

        return f"""
        <article class="card card-flag">
          <div class="card-top">
            <div>
              <div class="card-eyebrow">{_h(f['opportunity_id'])} · {_h(customer)}</div>
              <h3 class="card-title">{title}</h3>
            </div>
            {link_html}
          </div>
          <p class="card-action">{action}</p>
          {root_cause_html}
          {prior_resolution_html}
        </article>
        """

    flag_cards_html = "".join(_render_flag_card(f) for f in flags) if flags else '<div class="empty">Nothing flagged. The engine ran clean.</div>'

    # ---------- Decision cards ----------
    def _render_decision_card(d):
        customer = opp_customer.get(d["opportunity_id"], "")
        if d["winning_system"] == "salesforce":
            was_value, now_value = d["mc_value"], d["sf_value"]
            was_source, now_source = "Mission Control", "Salesforce"
        else:
            was_value, now_value = d["sf_value"], d["mc_value"]
            was_source, now_source = "Salesforce", "Mission Control"
        rule_label = "Ownership rule" if d["rule_applied"] == "ownership" else "Recency rule"
        was_display = _format_value_for_display(d["concept"], was_value)
        now_display = _format_value_for_display(d["concept"], now_value)
        variance_html = _variance_line(d["concept"], was_value, now_value)
        root_cause_html = _root_cause_line(d.get("root_cause_category"), d.get("likely_source"))
        return f"""
        <article class="card card-decision">
          <div class="card-top">
            <div>
              <div class="card-eyebrow">{_h(d['opportunity_id'])} · {_h(customer)}</div>
              <h3 class="card-title">{_h(_format_concept_name(d['concept']))}</h3>
            </div>
            <span class="pill pill-decision">{_h(rule_label)}</span>
          </div>
          <div class="values">
            <div class="values-col values-was">
              <small>Current · {_h(was_source)}</small>
              <div class="values-text">{_h(was_display)}</div>
            </div>
            <span class="values-arrow">→</span>
            <div class="values-col values-now">
              <small>Proposed · {_h(now_source)}</small>
              <div class="values-text">{_h(now_display)}</div>
            </div>
          </div>
          {variance_html}
          {root_cause_html}
          <p class="card-meta"><em>{_h(d['reason'])}</em></p>
        </article>
        """

    decision_cards_html = "".join(_render_decision_card(d) for d in decisions) if decisions else '<div class="empty">No corrections proposed this run.</div>'

    # ---------- Held cards (small) ----------
    def _render_held_card(h):
        customer = opp_customer.get(h["opportunity_id"], "")
        # Held items can come from either a field discrepancy or a missing-record
        # under its own threshold. Pick the one that matches so the eligibility
        # countdown is correct for both.
        reason = (h.get("reason") or "").lower()
        if "record present in" in reason:
            held_threshold = record_threshold_hrs
        else:
            held_threshold = field_threshold_hrs
        eligibility = _held_eligibility_line(h["reason"], RUN_TIMESTAMP, threshold_hours=held_threshold)
        return f"""
        <div class="card card-held">
          <div class="card-top">
            <div>
              <div class="card-eyebrow">{_h(h['opportunity_id'])} · {_h(customer)}</div>
              <h3 class="card-title">{_h(_format_concept_name(h['concept']))}</h3>
            </div>
            <span class="pill pill-held">Held</span>
          </div>
          <p class="card-meta">{_h(eligibility)}</p>
        </div>
        """

    held_cards_html = "".join(_render_held_card(h) for h in held) if held else '<div class="empty">Nothing on hold this run.</div>'

    # ---------- Raw data tables ----------
    def _render_data_table(df, highlight_col):
        headers = "".join(f"<th>{_h(col)}</th>" for col in df.columns)
        rows_html = []
        for _, row in df.iterrows():
            raw_id = str(row[highlight_col])
            clean_id = raw_id.replace("MC-", "")
            if clean_id in {e["opportunity_id"] for e in flags}:
                row_class = ' class="row-flag"'
            elif clean_id in {e["opportunity_id"] for e in decisions}:
                row_class = ' class="row-decision"'
            elif clean_id in {e["opportunity_id"] for e in held}:
                row_class = ' class="row-held"'
            else:
                row_class = ""
            cells = "".join(f"<td>{_h(row[col])}</td>" for col in df.columns)
            rows_html.append(f"<tr{row_class}>{cells}</tr>")
        return f"<table class='data-table'><thead><tr>{headers}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"

    sf_table = _render_data_table(sf_df, "opportunity_id")
    mc_table = _render_data_table(mc_df, "mc_project_id")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Deal Desk | Reconciliation — {_h(RUN_DATE_STR)}</title>
<style>
  :root {{
    --bg: #f6f8fb;
    --panel: #ffffff;
    --ink: #152239;
    --muted: #65758a;
    --subtle: #8995a8;
    --line: #e5eaf1;
    --blue: #3768d6;
    --blue-bg: #eaf0ff;
    --blue-text: #2857b3;
    --amber: #b67a26;
    --amber-bg: #fff5e6;
    --amber-text: #a46c1d;
    --red: #bf5d4b;
    --red-bg: #fff1ed;
    --red-text: #a44b3a;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  .shell {{ max-width: 1200px; margin: 0 auto; padding: 36px 24px 60px; }}
  .topline {{ display: flex; justify-content: space-between; align-items: center; gap: 20px; flex-wrap: wrap; }}
  .brand {{ font-size: 11px; letter-spacing: 0.12em; font-weight: 700; color: #3866b2; text-transform: uppercase; }}
  .runbadge {{
    display: inline-flex; align-items: center; gap: 8px;
    background: #e8f5f0; color: #277460;
    padding: 6px 12px; border-radius: 100px;
    font-size: 11px; font-weight: 700;
  }}
  .runbadge::before {{ content: ""; background: #299475; width: 7px; height: 7px; border-radius: 50%; }}
  h1 {{ font-size: 30px; letter-spacing: -0.03em; margin: 14px 0 6px; font-weight: 700; }}
  .lede {{ margin: 0; color: var(--muted); font-size: 14px; }}
  .banner {{
    margin: 20px 0 0;
    padding: 14px 18px;
    background: #fff8e6;
    border: 1px solid #f0d78a;
    border-left: 3px solid var(--amber);
    border-radius: 8px;
    font-size: 13px;
    color: #6b4e00;
  }}
  .banner strong {{ color: #4a3700; }}
  .stats {{
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 14px;
    margin: 28px 0 0;
  }}
  .stat {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 18px 20px;
  }}
  .stat .label {{ color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }}
  .stat .num {{ font-size: 32px; line-height: 1.15; letter-spacing: -0.03em; font-weight: 700; margin: 8px 0 4px; }}
  .stat small {{ color: var(--subtle); font-size: 11px; }}
  .stat.flag .num {{ color: var(--red); }}
  .stat.decision .num {{ color: var(--blue); }}
  .layout {{
    display: grid;
    grid-template-columns: minmax(0, 1.9fr) minmax(260px, 0.85fr);
    gap: 20px;
    align-items: start;
    margin-top: 28px;
  }}
  @media (max-width: 900px) {{
    .layout {{ grid-template-columns: 1fr; }}
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
  }}
  .panel {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 22px;
  }}
  .panel + .panel {{ margin-top: 20px; }}
  .section-head {{ margin-bottom: 14px; }}
  .section-head h2 {{ margin: 0; font-size: 16px; letter-spacing: -0.01em; font-weight: 650; }}
  .section-head .caption {{ color: var(--muted); font-size: 12px; margin-top: 3px; }}
  .card {{ padding: 16px 0; border-bottom: 1px solid var(--line); }}
  .card:last-child {{ border-bottom: 0; padding-bottom: 0; }}
  .card:first-child {{ padding-top: 0; }}
  .card-top {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 14px; }}
  .card-eyebrow {{ font-size: 11px; color: var(--muted); font-weight: 600; letter-spacing: 0.02em; }}
  .card-title {{ font-size: 15px; margin: 4px 0 0; font-weight: 650; letter-spacing: -0.01em; }}
  .card-action {{ margin: 10px 0 8px; color: #4b5a6e; font-size: 13px; }}
  .card-meta {{ font-size: 12px; color: var(--muted); margin: 10px 0 0; }}
  .card-btn {{
    display: inline-block;
    padding: 7px 13px;
    background: var(--ink);
    color: #fff;
    text-decoration: none;
    border-radius: 7px;
    font-size: 12px;
    font-weight: 500;
    white-space: nowrap;
  }}
  .card-btn:hover {{ opacity: 0.9; }}
  .pill {{
    font-size: 11px; white-space: nowrap;
    padding: 4px 10px; border-radius: 100px;
    font-weight: 600;
  }}
  .pill-decision {{ background: var(--blue-bg); color: var(--blue-text); }}
  .pill-held {{ background: var(--amber-bg); color: var(--amber-text); }}
  .pill-rootcause {{ background: #f3eef9; color: #6a4a95; }}
  .card-rootcause {{ margin: 10px 0 2px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .rootcause-text {{ font-size: 12px; color: var(--muted); flex: 1; min-width: 0; }}
  .rootcause-label {{ color: #6a4a95; font-weight: 600; }}
  .prior-resolution {{
    margin: 10px 0 0;
    padding: 10px 12px;
    background: var(--red-bg);
    border-left: 2px solid var(--red);
    border-radius: 6px;
    font-size: 12px;
    color: var(--red-text);
    line-height: 1.5;
  }}
  .prior-resolution strong {{ color: var(--red-text); font-weight: 650; }}
  .prior-resolution em {{ color: #7a3a2a; font-style: italic; }}
  .prior-label {{ color: #7a3a2a; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; margin-right: 4px; }}
  .prior-note {{ display: block; margin-top: 6px; font-size: 11px; color: #7a3a2a; font-style: italic; opacity: 0.85; }}
  .values {{
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    align-items: center;
    margin: 12px 0 8px;
    padding: 12px 14px;
    background: #f7f9fc;
    border: 1px solid #edf0f4;
    border-radius: 9px;
  }}
  .values-col {{ min-width: 0; }}
  .values-col small {{
    display: block;
    color: var(--subtle);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
    margin-bottom: 4px;
  }}
  .values-text {{ font-weight: 600; word-break: break-word; }}
  .values-was .values-text {{ color: var(--muted); text-decoration: line-through; text-decoration-color: var(--subtle); }}
  .values-arrow {{ color: var(--subtle); font-size: 16px; }}
  .variance {{ margin: 8px 0 0; font-size: 12px; color: var(--blue-text); font-weight: 600; }}
  .empty {{
    padding: 24px;
    text-align: center;
    color: var(--subtle);
    font-size: 13px;
    background: #fbfcfe;
    border: 1px dashed var(--line);
    border-radius: 9px;
  }}
  .card-held {{ padding: 14px 0; }}
  .card-flag {{ border-left: 3px solid var(--red); padding-left: 14px; }}
  .card-decision {{ border-left: 3px solid var(--blue); padding-left: 14px; }}
  .policy-row {{
    display: flex;
    justify-content: space-between;
    padding: 10px 0;
    border-bottom: 1px solid var(--line);
    gap: 12px;
    font-size: 13px;
  }}
  .policy-row:last-child {{ border-bottom: 0; padding-bottom: 0; }}
  .policy-row span {{ color: var(--muted); }}
  .policy-row strong {{ color: var(--ink); font-weight: 600; }}
  .policy-note {{ font-size: 11px; color: var(--subtle); margin: 10px 0 0; }}
  details.raw {{
    margin-top: 24px;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 18px 22px;
  }}
  details.raw summary {{
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
    color: #45607e;
    user-select: none;
  }}
  details.raw[open] summary {{ margin-bottom: 16px; }}
  .raw-tables {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  @media (max-width: 900px) {{ .raw-tables {{ grid-template-columns: 1fr; }} }}
  .raw-table-title {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--subtle); margin: 0 0 6px; font-weight: 600; }}
  .raw-table-wrap {{ overflow-x: auto; }}
  table.data-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  table.data-table th, table.data-table td {{ padding: 7px 9px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }}
  table.data-table th {{ font-weight: 600; color: var(--subtle); font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; }}
  tr.row-decision td {{ background: var(--blue-bg); }}
  tr.row-decision td:first-child {{ border-left: 2px solid var(--blue); font-weight: 600; }}
  tr.row-flag td {{ background: var(--red-bg); }}
  tr.row-flag td:first-child {{ border-left: 2px solid var(--red); font-weight: 600; }}
  tr.row-held td {{ background: var(--amber-bg); }}
  tr.row-held td:first-child {{ border-left: 2px solid var(--amber); font-weight: 600; }}
  footer {{
    margin-top: 32px;
    padding-top: 20px;
    border-top: 1px solid var(--line);
    color: var(--muted);
    font-size: 12px;
  }}
  footer strong {{ color: var(--ink); }}
  footer code {{
    background: #f0f3f8;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 11px;
    color: #45607e;
  }}
</style>
</head>
<body>
<main class="shell">

  <div class="topline">
    <span class="brand">Deal Desk · Reconciliation</span>
    <span class="runbadge">Run complete</span>
  </div>

  <h1>Reconciliation overview</h1>
  <p class="lede">Salesforce ↔ Mission Control · {_h(RUN_DATE_STR)} · Read-only report</p>

  <div class="banner">
    <strong>Demo artifact.</strong> The engine proposes corrections based on the SOP. It does not write back to Salesforce or Mission Control in this build — a write-back module is the next engineering step. See README for the full list of known limitations.
  </div>

  <section class="stats" aria-label="Summary">
    <div class="stat">
      <div class="label">Opportunities in scope</div>
      <div class="num">{opps_in_scope}</div>
      <small>Unique IDs across both systems</small>
    </div>
    <div class="stat flag">
      <div class="label">Needing review</div>
      <div class="num">{unique_flagged_opps}</div>
      <small>{len(flags)} flagged discrepancies</small>
    </div>
    <div class="stat decision">
      <div class="label">Corrections proposed</div>
      <div class="num">{len(decisions)}</div>
      <small>By ownership or recency rule</small>
    </div>
    <div class="stat">
      <div class="label">Held</div>
      <div class="num">{len(held)}</div>
      <small>Stabilization: {threshold_summary}</small>
    </div>
  </section>

  <div class="layout">

    <div>
      <section class="panel">
        <div class="section-head">
          <h2>Needs review</h2>
          <div class="caption">Human decision required. Each item links to its Salesforce record.</div>
        </div>
        {flag_cards_html}
      </section>

      <section class="panel">
        <div class="section-head">
          <h2>Corrections proposed</h2>
          <div class="caption">Decisions the engine would apply if the write-back layer were enabled.</div>
        </div>
        {decision_cards_html}
      </section>

      <section class="panel">
        <div class="section-head">
          <h2>Held while they stabilize</h2>
          <div class="caption">Changes still inside their stabilization window ({threshold_summary}). Re-evaluated next run.</div>
        </div>
        {held_cards_html}
      </section>
    </div>

    <aside>
      <section class="panel">
        <div class="section-head">
          <h2>Decision policy</h2>
          <div class="caption">The SOP the engine applies, in one glance.</div>
        </div>
        <div class="policy-row"><span>Field ownership</span><strong>Owner wins</strong></div>
        <div class="policy-row"><span>Shared fields</span><strong>Newest record*</strong></div>
        <div class="policy-row"><span>Unresolved SOP gaps</span><strong>Human review</strong></div>
        <div class="policy-row"><span>Stabilization</span><strong>{threshold_summary}</strong></div>
        <p class="policy-note">*Current recency logic uses record-level timestamps, not field-level change history. See README.</p>
      </section>

      <section class="panel">
        <div class="section-head">
          <h2>Role of each surface</h2>
        </div>
        <div class="policy-row"><span>Slack</span><strong>Notification</strong></div>
        <div class="policy-row"><span>Salesforce</span><strong>Operational resolution</strong></div>
        <div class="policy-row"><span>This dashboard</span><strong>Observability</strong></div>
        <p class="policy-note">Keeping these separate means operators never adopt a new tool to do their work.</p>
      </section>
    </aside>

  </div>

  <details class="raw">
    <summary>Show raw system state (inputs, color-coded by action)</summary>
    <div class="raw-tables">
      <div>
        <div class="raw-table-title">Salesforce export</div>
        <div class="raw-table-wrap">{sf_table}</div>
      </div>
      <div>
        <div class="raw-table-title">Mission Control export</div>
        <div class="raw-table-wrap">{mc_table}</div>
      </div>
    </div>
  </details>

  <footer>
    <strong>Prototype, not production.</strong> Run ID: <code>{_h(RUN_ID)}</code>. Full audit log at <code>output/reconciliation_log.json</code>. In production, Slack is the notification channel, Salesforce is where resolution happens as a custom object, and this dashboard is observability — for people who want to understand the system, not operate it.
  </footer>

</main>
</body>
</html>
"""

    with open(DASHBOARD_HTML, "w") as f:
        f.write(html)

    return {"dashboard_path": str(DASHBOARD_HTML)}


# -------------------------------------------------------------------
# Small helpers
# -------------------------------------------------------------------

def _serialize(value):
    """Convert pandas/numpy types to JSON-serializable Python types."""
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _count_opportunities_in_current_run(log):
    current = {entry["opportunity_id"] for entry in log if entry.get("run_id") == RUN_ID}
    return len(current) if current else 0


def _pluralize(singular, plural, count):
    return singular if count == 1 else plural


_SF_BASE_URL = None


def _build_opp_link(opp_id, sf_record_id):
    if _SF_BASE_URL and sf_record_id and not pd.isna(sf_record_id):
        return f"<{_SF_BASE_URL}/{sf_record_id}/view|{opp_id}>"
    return opp_id


def _post_to_slack(message):
    """
    Post a message to Slack via Incoming Webhook when SLACK_WEBHOOK_URL is
    set in the environment. Returns True on success, False on any failure
    or when the webhook is not configured. Never raises: the local text
    file write and the audit log are the sources of truth, so a failed
    notification should not abort the run.
    """
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return False
    try:
        response = requests.post(
            webhook_url,
            json={"text": message},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"  Warning: Slack webhook post failed: {exc}")
        return False


# -------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    arg_parser = argparse.ArgumentParser(description="Run the reconciliation engine.")
    arg_parser.add_argument(
        "--now",
        default=None,
        help=(
            "Override the simulated_now from contract.yaml with this ISO 8601 "
            "timestamp (must include a timezone, e.g. 2026-10-30T13:30:00Z). "
            "Useful for demonstrating the full detect-fix-recur loop without "
            "editing contract.yaml between runs."
        ),
    )
    cli_args = arg_parser.parse_args()

    data = load_data()
    _set_run_timestamp(data["contract"], override=cli_args.now)
    _set_salesforce_base_url(data["contract"])
    joined = join_records(
        data["sf_df"],
        data["mc_df"],
        data["mapping"]["shared_key"]["salesforce"],
    )
    discrepancies = classify_discrepancies(joined, data["contract"], data["mapping"])
    result = emit_outputs(discrepancies)
    weekly_result = emit_weekly_rollup()
    dashboard_result = emit_dashboard()

    print(f"\nRun complete. (run_id: {RUN_ID[:8]}...)")
    print(f"  Log entries written: {result['log_entries_written']}")
    posted_suffix = " (posted to Slack)" if result.get("slack_posted") else " (webhook not configured)"
    print(f"  Slack message: {result['slack_message_path']}{posted_suffix}")
    print(f"  Flags: {result['flags']}")
    print(f"  Corrections proposed: {result['auto_corrections']}")
    print(f"  Held: {result['held']}")
    print(f"\n--- Slack message contents ---")
    with open(result['slack_message_path']) as f:
        print(f.read())
    if weekly_result:
        print(f"\n--- Weekly rollup contents ---")
        with open(weekly_result['weekly_message_path']) as f:
            print(f.read())
    if dashboard_result:
        print(f"\nDashboard written: {dashboard_result['dashboard_path']}")
