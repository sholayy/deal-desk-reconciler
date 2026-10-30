"""
Deal Desk Reconciliation

Reads daily exports from Salesforce and Mission Control, applies the SOP
(contract.yaml), and produces three outputs:
  - A daily Slack message (output/slack/)
  - An HTML dashboard (output/dashboard.html)
  - An append-only JSON audit log (output/reconciliation_log.json)

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


def _set_run_timestamp(contract):
    """Sets the global run timestamp and a unique run ID."""
    global RUN_TIMESTAMP, RUN_DATE_STR, RUN_ID
    simulated = contract.get("simulated_now")
    if simulated:
        RUN_TIMESTAMP = date_parser.parse(simulated)
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

def classify_discrepancies(joined, contract, mapping):
    """
    For each joined row, inspects every mapped concept field and returns
    a list of classified discrepancies.

    Each discrepancy carries bucket, rule_applied, action, winning_system,
    and the SOP's reason. 'action' is a DECISION, not an executed write.
    """

    discrepancies = []
    threshold_hours = contract["persistence_threshold_hours"]

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
                if age_hours < threshold_hours:
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
                            f"({age_hours:.1f}h old); below {threshold_hours}h "
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

            if age_hours < threshold_hours:
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
                    "reason": f"Most recent change {age_hours:.1f}h ago; below {threshold_hours}h persistence threshold.",
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

    return discrepancies


# -------------------------------------------------------------------
# Emit — writes the audit log (append-only) and the Slack message.
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
# Weekly rollup — dedupes to one run per day so repeated executions
# don't inflate counts.
# -------------------------------------------------------------------

def emit_weekly_rollup():
    if not AUDIT_LOG.exists():
        return None
    with open(AUDIT_LOG, "r") as f:
        log = json.load(f)

    from datetime import timedelta
    cutoff = RUN_TIMESTAMP - timedelta(days=7)
    all_recent = [
        e for e in log
        if date_parser.parse(e["run_timestamp"]) >= cutoff
    ]
    if not all_recent:
        return None

    from collections import defaultdict
    days = defaultdict(list)
    for e in all_recent:
        days[e["run_date"]].append(e)
    latest_run_per_day = {}
    for date, entries in days.items():
        latest = max(entries, key=lambda e: e["run_timestamp"])
        latest_run_per_day[date] = latest.get("run_id")
    recent = [
        e for e in all_recent
        if e.get("run_id") == latest_run_per_day.get(e["run_date"])
    ]
    if not recent:
        return None

    from collections import Counter
    bucket_counts = Counter(e["bucket"] for e in recent)
    recency_fields = Counter(
        e["concept"] for e in recent if e["rule_applied"] == "recency"
    )
    gaps_this_week = set(
        e["concept"] for e in recent if e["bucket"] == "contract_gap"
    )

    week_header = RUN_TIMESTAMP.strftime("Week ending %A %b %-d")
    lines = [f"*Deal Desk reconciliation — weekly rollup: {week_header}*", ""]

    total = len(recent)
    corrections = bucket_counts.get("owned_field", 0) + bucket_counts.get("shared_field", 0)
    flags = bucket_counts.get("missing_record", 0) + bucket_counts.get("contract_gap", 0)
    lines.append(
        f"{total} discrepancies this week. "
        f"{corrections} corrections proposed by rule, {flags} flagged for review, "
        f"{bucket_counts.get('below_threshold', 0)} held below threshold."
    )
    lines.append("")

    if recency_fields:
        lines.append("*Fields drifting most (recency decisions):*")
        for field, count in recency_fields.most_common(3):
            lines.append(f"  • {field}: {count} {'correction' if count == 1 else 'corrections'}")
        lines.append("")
        top_field, top_count = recency_fields.most_common(1)[0]
        if top_count >= 2:
            lines.append(
                f"_Pattern worth watching: {top_field} drifted {top_count} times this week. "
                f"Likely an upstream process gap — raise in next ops sync._"
            )
            lines.append("")

    if gaps_this_week:
        lines.append("*Contract gaps surfaced this week (SOP needs decisions):*")
        for gap in gaps_this_week:
            lines.append(f"  • {gap}")
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
        "entries_covered": len(recent),
        "fields_drifting": dict(recency_fields),
        "gaps_open": list(gaps_this_week),
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

    # Read the stabilization window from the SOP so a change in contract.yaml
    # flows through to every surface — dashboard header, policy sidebar,
    # held-item caption, eligibility countdown — without touching the engine.
    with open(CONTRACT_YAML, "r") as f:
        threshold = yaml.safe_load(f)["persistence_threshold_hours"]

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
            runbook = "Missing record"
        else:
            title = f"Policy gap: {_h(_format_concept_name(f['concept']))}"
            action = "SOP has not named an owner for this field. Decide and update contract.yaml."
            runbook = "Contract gap"
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
          <div class="card-meta">Runbook: {_h(runbook)}</div>
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
          <p class="card-meta"><em>{_h(d['reason'])}</em></p>
        </article>
        """

    decision_cards_html = "".join(_render_decision_card(d) for d in decisions) if decisions else '<div class="empty">No corrections proposed this run.</div>'

    # ---------- Held cards (small) ----------
    def _render_held_card(h):
        customer = opp_customer.get(h["opportunity_id"], "")
        eligibility = _held_eligibility_line(h["reason"], RUN_TIMESTAMP, threshold_hours=threshold)
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
      <small>{threshold}-hour stabilization window</small>
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
          <div class="caption">Changes less than the {threshold}-hour threshold. Re-evaluated next run.</div>
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
        <div class="policy-row"><span>Stabilization</span><strong>{threshold} hours</strong></div>
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
    data = load_data()
    _set_run_timestamp(data["contract"])
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
