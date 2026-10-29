"""
Deal Desk Reconciliation

Reads daily exports from Salesforce and Mission Control, applies the SOP
(contract.yaml), and produces three outputs:
  - A daily Slack message (output/slack/)
  - An HTML dashboard (output/dashboard.html)
  - An append-only JSON audit log (output/reconciliation_log.json)

Run: python reconcile.py
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
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
# Run metadata — today's date drives the Slack filename and log entries.
# -------------------------------------------------------------------

RUN_TIMESTAMP = None
RUN_DATE_STR = None
def _set_run_timestamp(contract):
    """Sets the global run timestamp, using simulated_now if configured."""
    global RUN_TIMESTAMP, RUN_DATE_STR
    simulated = contract.get("simulated_now")
    if simulated:
        RUN_TIMESTAMP = date_parser.parse(simulated)
    else:
        RUN_TIMESTAMP = datetime.now(timezone.utc)
    RUN_DATE_STR = RUN_TIMESTAMP.strftime("%Y-%m-%d")


def _set_salesforce_base_url(contract):
    """Sets the module-level Salesforce base URL from config."""
    global _SF_BASE_URL
    _SF_BASE_URL = contract.get("salesforce_base_url")
# -------------------------------------------------------------------
def load_data():
    """
    Reads the two CSV exports and the two YAML configs.

    Applies the field mapping so Mission Control columns are renamed to
    the SOP's concept names. From this point onwards, both dataframes
    speak the same vocabulary.

    Returns a dict with:
      - sf_df: Salesforce dataframe, columns renamed to concept names
      - mc_df: Mission Control dataframe, columns renamed to concept names
      - contract: parsed contract.yaml as a dict
      - mapping: parsed field_mapping.yaml as a dict
    """

    # Load the configs first — if these are broken, nothing else matters.
    with open(CONTRACT_YAML, "r") as f:
        contract = yaml.safe_load(f)

    with open(FIELD_MAPPING_YAML, "r") as f:
        mapping = yaml.safe_load(f)

    # Load both CSVs.
    sf_df = pd.read_csv(SALESFORCE_CSV)
    mc_df = pd.read_csv(MISSION_CONTROL_CSV)

    # Strip the "MC-" prefix from Mission Control IDs so the join works.
    mc_prefix = mapping["shared_key"]["mc_prefix"]
    mc_id_col = mapping["shared_key"]["mission_control"]
    mc_df[mc_id_col] = mc_df[mc_id_col].str.replace(mc_prefix, "", regex=False)

    # Rename the Mission Control ID column to match Salesforce so the join
    # has a single column name to work with.
    sf_id_col = mapping["shared_key"]["salesforce"]
    mc_df = mc_df.rename(columns={mc_id_col: sf_id_col})

    # Rename Mission Control's timestamp column to a single shared name.
    sf_ts_col = mapping["timestamps"]["salesforce"]
    mc_ts_col = mapping["timestamps"]["mission_control"]
    mc_df = mc_df.rename(columns={mc_ts_col: f"{sf_ts_col}_mc"})
    sf_df = sf_df.rename(columns={sf_ts_col: f"{sf_ts_col}_sf"})

    # Rename every mapped concept field so both dataframes speak the same
    # vocabulary from here on. We suffix with _sf and _mc so we can tell
    # which value came from which system after the join.
    concept_rename_sf = {}
    concept_rename_mc = {}

    for concept, cols in mapping["concepts"].items():
        if cols["salesforce"] is not None:
            concept_rename_sf[cols["salesforce"]] = f"{concept}_sf"
        if cols["mission_control"] is not None:
            concept_rename_mc[cols["mission_control"]] = f"{concept}_mc"

    sf_df = sf_df.rename(columns=concept_rename_sf)
    mc_df = mc_df.rename(columns=concept_rename_mc)

    # Same treatment for the contract gap fields.
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

    Outer join is deliberate: records present in only one system
    must survive the join so the classification step can flag them.
    An inner join would silently drop missing records, which is
    the exact failure mode this system exists to prevent.

    Returns a single dataframe where each row has both the _sf and
    _mc version of every mapped field. NaN on either side indicates
    the record is missing in that system.
    """

    joined = pd.merge(
        sf_df,
        mc_df,
        on=shared_key_col,
        how="outer",
        indicator=True,
    )

    # `_merge` is a pandas-provided column that tells us which side
    # each row came from: 'both', 'left_only' (SF only), 'right_only' (MC only).
    # We rename it to something more meaningful for downstream code.
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
# -------------------------------------------------------------------

def classify_discrepancies(joined, contract, mapping):
    """
    For each joined row, inspects every mapped concept field and
    returns a list of classified discrepancies.

    Each discrepancy carries:
      - opportunity_id
      - concept: the SOP field name
      - sf_value: Salesforce value
      - mc_value: Mission Control value
      - bucket: one of 'owned_field', 'shared_field', 'missing_record',
                'contract_gap', 'below_threshold'
      - rule_applied: 'ownership' | 'recency' | 'none'
      - action: 'auto_correct' | 'flag' | 'hold'
      - winning_system: for auto-corrections, which system's value wins
      - reason: the SOP's one-line rationale

    Does not act on anything. Only classifies. Downstream functions
    apply the actions and write the outputs.
    """

    discrepancies = []
    threshold_hours = contract["persistence_threshold_hours"]

    for _, row in joined.iterrows():
        opp_id = row["opportunity_id"]

        # ---------------------------------------------------------
        # Bucket 3: missing record. Short-circuit the rest — no
        # point comparing fields when half the record isn't there.
        # ---------------------------------------------------------
        if row["presence"] != "both":
            missing_from = (
                "mission_control"
                if row["presence"] == "salesforce_only"
                else "salesforce"
            )
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
                "reason": f"Record present in {row['presence'].replace('_only', '')} only; missing from {missing_from}.",
            })
            continue

        # ---------------------------------------------------------
        # Walk every mapped concept field.
        # ---------------------------------------------------------
        for concept, field_rule in contract["fields"].items():
            sf_col = f"{concept}_sf"
            mc_col = f"{concept}_mc"

            # Skip fields that only exist in one system — no comparison possible.
            if field_rule.get("salesforce_only") or field_rule.get("mission_control_only"):
                continue

            sf_val = row.get(sf_col)
            mc_val = row.get(mc_col)

            # If either side is missing the value entirely, skip —
            # that's a data quality issue, not a reconciliation one.
            if pd.isna(sf_val) or pd.isna(mc_val):
                continue

            # Values agree — nothing to do.
            if sf_val == mc_val:
                continue

            # ---------------------------------------------------------
            # Check persistence threshold. If the most recent update
            # on either side is too recent, hold for next run.
            # ---------------------------------------------------------
            sf_ts = date_parser.parse(row["last_modified_ts_sf"])
            mc_ts = date_parser.parse(row["last_modified_ts_mc"])
            most_recent = max(sf_ts, mc_ts)
            age_hours = (RUN_TIMESTAMP - most_recent).total_seconds() / 3600

            if age_hours < threshold_hours:
                discrepancies.append({
                    "opportunity_id": opp_id,
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

            # ---------------------------------------------------------
            # Bucket 1: owned field. The owner's value wins.
            # ---------------------------------------------------------
            if field_rule["rule"] == "ownership":
                owner = field_rule["owner"]
                winning_value = sf_val if owner == "salesforce" else mc_val
                losing_system = "mission_control" if owner == "salesforce" else "salesforce"

                discrepancies.append({
                    "opportunity_id": opp_id,
                    "concept": concept,
                    "sf_value": sf_val,
                    "mc_value": mc_val,
                    "bucket": "owned_field",
                    "rule_applied": "ownership",
                    "action": "auto_correct",
                    "winning_system": owner,
                    "reason": field_rule["reason"],
                })

            # ---------------------------------------------------------
            # Bucket 2: shared field. Recency wins.
            # ---------------------------------------------------------
            elif field_rule["rule"] == "recency":
                winning_system = "salesforce" if sf_ts > mc_ts else "mission_control"

                discrepancies.append({
                    "opportunity_id": opp_id,
                    "concept": concept,
                    "sf_value": sf_val,
                    "mc_value": mc_val,
                    "bucket": "shared_field",
                    "rule_applied": "recency",
                    "action": "auto_correct",
                    "winning_system": winning_system,
                    "reason": field_rule["reason"],
                })

        # ---------------------------------------------------------
        # Bucket 4: contract gaps. Check every gap field, flag if
        # the two systems disagree.
        # ---------------------------------------------------------
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
# -------------------------------------------------------------------

def emit_outputs(discrepancies):
    """
    Writes two outputs for this run:
      - Appends every discrepancy to reconciliation_log.json (audit trail)
      - Writes a daily Slack message to output/slack/slack_YYYY-MM-DD.txt

    The Slack message follows the templates defined in the SOP:
      - Clean day: short status, no flags
      - Flagged day: lead with flag count, then each flag with action
    """

    # ---------------------------------------------------------------
    # Audit log — append every classified discrepancy as its own row.
    # ---------------------------------------------------------------

    log_entries = []
    for d in discrepancies:
        log_entries.append({
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

    # Load existing log if it exists, append new entries, write back.
    existing_log = []
    if AUDIT_LOG.exists():
        with open(AUDIT_LOG, "r") as f:
            existing_log = json.load(f)

    existing_log.extend(log_entries)

    with open(AUDIT_LOG, "w") as f:
        json.dump(existing_log, f, indent=2)

    # ---------------------------------------------------------------
    # Slack message — format based on what the engine saw this run.
    # ---------------------------------------------------------------

    auto_corrections = [d for d in discrepancies if d["action"] == "auto_correct"]
    flags = [d for d in discrepancies if d["action"] == "flag"]
    held = [d for d in discrepancies if d["action"] == "hold"]

    date_header = RUN_TIMESTAMP.strftime("%A %b %-d")

    lines = [f"*Deal Desk reconciliation — {date_header}*", ""]

    if not flags:
        # Clean-day template
        total_compared = _count_opportunities_in_log(existing_log)
        lines.append(
            f"{total_compared} opportunities compared. "
            f"{len(auto_corrections)} silent corrections. "
            f"Nothing needs attention."
        )
    else:
        # Flagged-day template
        lines.append(f"{len(flags)} {_pluralize('opportunity', 'opportunities', len(flags))} need attention.")
        lines.append("")

        for flag in flags:
            opp_link = _build_opp_link(flag["opportunity_id"], flag.get("sf_record_id"))
            if flag["bucket"] == "missing_record":
                lines.append(
                    f"*{opp_link}* — Missing Mission Control record. "
                    f"{flag['reason']} Runbook: Missing Mission Control record."
                )
            elif flag["bucket"] == "contract_gap":
                lines.append(
                    f"*{opp_link}* — {flag['concept']} fields disagree between systems. "
                    f"SOP does not name an owner. Runbook: Contract gap."
                )
            lines.append("")

        if auto_corrections:
            lines.append(f"{len(auto_corrections)} silent corrections also applied.")

    if held:
        lines.append(f"({len(held)} held below persistence threshold.)")

    lines.append("")
    lines.append(f"Weekly rollup: output/slack/weekly_{RUN_DATE_STR}.txt")

    slack_path = SLACK_DIR / f"slack_{RUN_DATE_STR}.txt"
    with open(slack_path, "w") as f:
        f.write("\n".join(lines))

    return {
        "log_entries_written": len(log_entries),
        "slack_message_path": str(slack_path),
        "flags": len(flags),
        "auto_corrections": len(auto_corrections),
        "held": len(held),
    }


# -------------------------------------------------------------------
# Weekly rollup — richer Slack message generated once a week.
# Reads the audit log for the last 7 days and surfaces patterns.
# -------------------------------------------------------------------

def emit_weekly_rollup():
    """
    Reads the last 7 days of audit log entries and generates a
    weekly Slack rollup message. Covers:
      - Count totals by bucket for the week
      - Fields that drifted most often (recency corrections by field)
      - Any contract gaps that stayed open across the week
      - A short commentary line on what's trending

    Writes to output/slack/weekly_YYYY-MM-DD.txt.
    Only called on Monday runs in production; for the demo we always generate.
    """

    if not AUDIT_LOG.exists():
        return None

    with open(AUDIT_LOG, "r") as f:
        log = json.load(f)

    from datetime import timedelta
    cutoff = RUN_TIMESTAMP - timedelta(days=7)
    recent = [
        e for e in log
        if date_parser.parse(e["run_timestamp"]) >= cutoff
    ]

    if not recent:
        return None

    from collections import Counter
    bucket_counts = Counter(e["bucket"] for e in recent)

    recency_fields = Counter(
        e["concept"] for e in recent
        if e["rule_applied"] == "recency"
    )

    gaps_this_week = set(
        e["concept"] for e in recent
        if e["bucket"] == "contract_gap"
    )

    week_header = RUN_TIMESTAMP.strftime("Week ending %A %b %-d")
    lines = [f"*Deal Desk reconciliation — weekly rollup: {week_header}*", ""]

    total = len(recent)
    corrections = bucket_counts.get("owned_field", 0) + bucket_counts.get("shared_field", 0)
    flags = bucket_counts.get("missing_record", 0) + bucket_counts.get("contract_gap", 0)
    lines.append(
        f"{total} discrepancies this week. "
        f"{corrections} auto-corrected, {flags} flagged, "
        f"{bucket_counts.get('below_threshold', 0)} held below threshold."
    )
    lines.append("")

    if recency_fields:
        lines.append("*Fields drifting most (recency corrections):*")
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
    with open(weekly_path, "w") as f:
        f.write("\n".join(lines))

    return {
        "weekly_message_path": str(weekly_path),
        "entries_covered": len(recent),
        "fields_drifting": dict(recency_fields),
        "gaps_open": list(gaps_this_week),
    }
def emit_dashboard():
    """
    Writes a command-center style HTML dashboard.
    Structured as a Monday-morning briefing: action first, then what the
    engine did, then supporting data on request.
    """

    import re
    from collections import Counter

    sf_df = pd.read_csv(SALESFORCE_CSV)
    mc_df = pd.read_csv(MISSION_CONTROL_CSV)

    with open(AUDIT_LOG, "r") as f:
        log = json.load(f)

    todays_entries = [e for e in log if e["run_date"] == RUN_DATE_STR]

    # Lookups so we can show customer names and link to the actual Salesforce record.
    opp_customer = dict(zip(sf_df["opportunity_id"], sf_df["customer_name"]))
    opp_sf_record = dict(zip(sf_df["opportunity_id"], sf_df["sf_record_id"]))

    auto_corrections = [e for e in todays_entries if e["action"] == "auto_correct"]
    flags = [e for e in todays_entries if e["action"] == "flag"]
    held = [e for e in todays_entries if e["action"] == "hold"]

    # ---------- Status band ----------
    status_parts = []
    if flags:
        status_parts.append(f"<strong>{len(flags)} need attention</strong>")
    if auto_corrections:
        status_parts.append(f"{len(auto_corrections)} corrected silently")
    if held:
        status_parts.append(f"{len(held)} held while they stabilize")
    status_line = " · ".join(status_parts) if status_parts else "No activity today."

    # ---------- Flag cards ----------
    flag_cards = []
    for f in flags:
        customer = opp_customer.get(f["opportunity_id"], "")
        sf_record = opp_sf_record.get(f["opportunity_id"], "")
        link = f"{_SF_BASE_URL}/{sf_record}/view" if _SF_BASE_URL and sf_record else "#"

        if f["bucket"] == "missing_record":
            title = "Missing from Mission Control"
            action = "Open the delivery record, or confirm the engagement was cancelled."
            runbook = "Missing Mission Control record"
        else:
            title = f"Contract gap: {f['concept']}"
            action = "SOP does not name an owner for this field. Raise in next ops sync."
            runbook = "Contract gap"

        flag_cards.append(f"""
        <div class="card card-flag">
          <div class="card-top">
            <div>
              <div class="card-opp">{f['opportunity_id']} — {customer}</div>
              <div class="card-title">{title}</div>
            </div>
            <a href="{link}" class="btn" target="_blank">Open in Salesforce</a>
          </div>
          <div class="card-action">{action}</div>
          <div class="card-meta">Runbook: {runbook}</div>
        </div>
        """)

    flag_cards_html = "".join(flag_cards) if flag_cards else '<div class="empty-state">Nothing flagged. The engine ran clean.</div>'

    # ---------- Correction cards ----------
    correction_cards = []
    for c in auto_corrections:
        customer = opp_customer.get(c["opportunity_id"], "")
        if c["winning_system"] == "salesforce":
            was_value = c["mc_value"]
            now_value = c["sf_value"]
            was_source = "Mission Control"
            now_source = "Salesforce"
        else:
            was_value = c["sf_value"]
            now_value = c["mc_value"]
            was_source = "Salesforce"
            now_source = "Mission Control"

        rule_label = "Ownership rule" if c["rule_applied"] == "ownership" else "Recency rule"

        correction_cards.append(f"""
        <div class="card card-correct">
          <div class="card-top">
            <div>
              <div class="card-opp">{c['opportunity_id']} — {customer}</div>
              <div class="card-title">{c['concept']}</div>
            </div>
            <div class="rule-pill">{rule_label}</div>
          </div>
          <div class="ba-row">
            <div class="ba-col ba-was">
              <div class="ba-label">Was · {was_source}</div>
              <div class="ba-value">{was_value}</div>
            </div>
            <div class="ba-arrow">→</div>
            <div class="ba-col ba-now">
              <div class="ba-label">Now · {now_source}</div>
              <div class="ba-value">{now_value}</div>
            </div>
          </div>
          <div class="card-meta">{c['reason']}</div>
        </div>
        """)

    correction_cards_html = "".join(correction_cards) if correction_cards else '<div class="empty-state">No corrections applied today.</div>'

    # ---------- Trend text ----------
    recency_fields = Counter(e["concept"] for e in todays_entries if e["rule_applied"] == "recency")
    if recency_fields:
        top_field, top_count = recency_fields.most_common(1)[0]
        trend_text = f"Most-drifted field today: <strong>{top_field}</strong> ({top_count} correction{'s' if top_count > 1 else ''}). If this keeps appearing week after week, the fix lives upstream in the GTM process, not in reconciliation."
    else:
        trend_text = "No recurring drift detected today."

    # ---------- Raw data (collapsed) ----------
    def _render_table(df, highlight_col):
        headers = "".join(f"<th>{col}</th>" for col in df.columns)
        rows = []
        for _, row in df.iterrows():
            opp_id = str(row[highlight_col])
            clean_id = opp_id.replace("MC-", "")
            if clean_id in {e["opportunity_id"] for e in auto_corrections}:
                cls = ' class="row-correct"'
            elif clean_id in {e["opportunity_id"] for e in flags}:
                cls = ' class="row-flag"'
            elif clean_id in {e["opportunity_id"] for e in held}:
                cls = ' class="row-hold"'
            else:
                cls = ""
            cells = "".join(f"<td>{row[col]}</td>" for col in df.columns)
            rows.append(f"<tr{cls}>{cells}</tr>")
        return f"<table class='data-table'><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"

    sf_table = _render_table(sf_df, "opportunity_id")
    mc_table = _render_table(mc_df, "mc_project_id")

    # ---------- HTML ----------
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deal Desk Reconciliation — {RUN_DATE_STR}</title>
<style>
  :root {{
    --bg: #f6f7f9;
    --panel: #ffffff;
    --text: #0f172a;
    --text-muted: #64748b;
    --text-subtle: #94a3b8;
    --border: #e2e8f0;
    --border-strong: #cbd5e1;
    --flag-bg: #fffbeb;
    --flag-border: #f59e0b;
    --flag-text: #92400e;
    --correct-bg: #eff6ff;
    --correct-border: #3b82f6;
    --correct-text: #1e40af;
    --hold-bg: #f1f5f9;
    --hold-border: #94a3b8;
    --accent: #0f172a;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 2.5rem 1.25rem;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 1040px; margin: 0 auto; }}
  header {{ margin-bottom: 2rem; }}
  h1 {{ font-size: 1.6rem; font-weight: 600; margin: 0 0 0.25rem 0; letter-spacing: -0.01em; }}
  .subtitle {{ color: var(--text-muted); font-size: 0.9rem; }}
  .status-band {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1.25rem 1.5rem;
    margin: 1.5rem 0 2.5rem 0;
    font-size: 1rem;
    color: var(--text);
  }}
  .status-band strong {{ color: var(--flag-text); }}
  section {{ margin-bottom: 2.5rem; }}
  h2 {{
    font-size: 0.78rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-subtle);
    margin: 0 0 0.9rem 0;
  }}
  .card {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1.1rem 1.3rem;
    margin-bottom: 0.75rem;
  }}
  .card-flag {{ border-left: 3px solid var(--flag-border); }}
  .card-correct {{ border-left: 3px solid var(--correct-border); }}
  .card-top {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    margin-bottom: 0.6rem;
  }}
  .card-opp {{ font-size: 0.78rem; color: var(--text-muted); font-weight: 500; letter-spacing: 0.02em; }}
  .card-title {{ font-size: 1rem; font-weight: 600; margin-top: 0.15rem; }}
  .card-action {{ font-size: 0.92rem; color: var(--text); margin-bottom: 0.5rem; }}
  .card-meta {{ font-size: 0.78rem; color: var(--text-subtle); }}
  .btn {{
    display: inline-block;
    padding: 0.4rem 0.85rem;
    background: var(--text);
    color: #fff;
    text-decoration: none;
    border-radius: 6px;
    font-size: 0.82rem;
    font-weight: 500;
    white-space: nowrap;
  }}
  .btn:hover {{ opacity: 0.9; }}
  .rule-pill {{
    display: inline-block;
    padding: 0.25rem 0.65rem;
    background: var(--correct-bg);
    color: var(--correct-text);
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 500;
    white-space: nowrap;
  }}
  .ba-row {{
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 0.75rem;
    align-items: center;
    margin: 0.75rem 0;
    padding: 0.85rem;
    background: var(--bg);
    border-radius: 8px;
  }}
  .ba-col {{ min-width: 0; }}
  .ba-label {{ font-size: 0.7rem; color: var(--text-subtle); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.25rem; }}
  .ba-value {{ font-size: 0.95rem; font-weight: 500; word-break: break-word; }}
  .ba-was .ba-value {{ color: var(--text-muted); text-decoration: line-through; text-decoration-color: var(--text-subtle); }}
  .ba-arrow {{ color: var(--text-subtle); font-size: 1.1rem; }}
  .empty-state {{
    padding: 1.25rem;
    text-align: center;
    color: var(--text-subtle);
    font-size: 0.9rem;
    background: var(--panel);
    border: 1px dashed var(--border);
    border-radius: 10px;
  }}
  .held-note, .trend-note {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem 1.25rem;
    font-size: 0.9rem;
    color: var(--text-muted);
  }}
  details.raw-data {{
    margin-top: 2.5rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem 1.25rem;
  }}
  details.raw-data summary {{
    cursor: pointer;
    font-size: 0.85rem;
    font-weight: 500;
    color: var(--text-muted);
    user-select: none;
  }}
  details.raw-data[open] summary {{ margin-bottom: 1.25rem; }}
  .raw-tables {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
  @media (max-width: 820px) {{ .raw-tables {{ grid-template-columns: 1fr; }} }}
  .raw-table-wrap {{ overflow-x: auto; }}
  .raw-table-title {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-subtle); margin: 0 0 0.5rem 0; font-weight: 500; }}
  table.data-table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; }}
  table.data-table th, table.data-table td {{ padding: 0.4rem 0.55rem; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }}
  table.data-table th {{ font-weight: 500; color: var(--text-subtle); font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.05em; }}
  tr.row-correct td {{ background: var(--correct-bg); }}
  tr.row-correct td:first-child {{ border-left: 2px solid var(--correct-border); }}
  tr.row-flag td {{ background: var(--flag-bg); }}
  tr.row-flag td:first-child {{ border-left: 2px solid var(--flag-border); }}
  tr.row-hold td {{ background: var(--hold-bg); }}
  tr.row-hold td:first-child {{ border-left: 2px solid var(--hold-border); }}
  footer {{
    margin-top: 2.5rem;
    padding-top: 1.5rem;
    border-top: 1px solid var(--border);
    color: var(--text-subtle);
    font-size: 0.8rem;
  }}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>Deal Desk Reconciliation</h1>
    <div class="subtitle">Run of {RUN_DATE_STR}</div>
  </header>

  <div class="status-band">{status_line}.</div>

  <section>
    <h2>Needs attention</h2>
    {flag_cards_html}
  </section>

  <section>
    <h2>Corrections applied silently</h2>
    {correction_cards_html}
  </section>

  <section>
    <h2>Held while they stabilize</h2>
    <div class="held-note">
      {len(held)} discrepanc{'y' if len(held) == 1 else 'ies'} had changes less than the 4-hour persistence threshold. The engine waits to confirm they persist before acting, so a mid-sync update does not create a false correction. Will re-check next run.
    </div>
  </section>

  <section>
    <h2>Pattern this week</h2>
    <div class="trend-note">{trend_text}</div>
  </section>

  <details class="raw-data">
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
    Demo artifact. In production, flags route to Salesforce as a custom object, the daily post lands in the Deal Desk Slack channel, and the audit log feeds the warehouse. Nothing new for the team to adopt.
  </footer>

</div>
</body>
</html>
"""

    with open(DASHBOARD_HTML, "w") as f:
        f.write(html)

    return {"dashboard_path": str(DASHBOARD_HTML)}

# -------------------------------------------------------------------
# Small helpers used by emit_outputs.
# -------------------------------------------------------------------

def _serialize(value):
    """Convert pandas/numpy types to JSON-serializable Python types."""
    if pd.isna(value):
        return None
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return value


def _count_opportunities_in_log(log):
    """Rough count of distinct opportunities touched today."""
    today = {entry["opportunity_id"] for entry in log if entry["run_date"] == RUN_DATE_STR}
    return len(today) if today else 0


def _pluralize(singular, plural, count):
    return singular if count == 1 else plural

# Module-level config loaded once for link building
_SF_BASE_URL = None


def _build_opp_link(opp_id, sf_record_id):
    """
    Returns a Slack-formatted link for an opportunity.
    If a Salesforce base URL is configured and the record has a Salesforce ID,
    returns a clickable <url|OPP-xxx> link. Otherwise returns the plain ID.
    """
    if _SF_BASE_URL and sf_record_id and not pd.isna(sf_record_id):
        return f"<{_SF_BASE_URL}/{sf_record_id}/view|{opp_id}>"
    return opp_id
# -------------------------------------------------------------------
# Temporary test — remove once we build the real main() function.
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

    print(f"\nRun complete.")
    print(f"  Log entries written: {result['log_entries_written']}")
    print(f"  Slack message: {result['slack_message_path']}")
    print(f"  Flags: {result['flags']}")
    print(f"  Auto-corrections: {result['auto_corrections']}")
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