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


# -------------------------------------------------------------------
# Dashboard — single static HTML page combining inputs, engine thinking,
# and outputs. Regenerated every run.
# -------------------------------------------------------------------

def emit_dashboard():
    """
    Reads the two input CSVs, the audit log, and both Slack messages,
    then writes a single HTML dashboard to output/dashboard.html.

    Three sections:
      1. The inputs — both CSVs rendered as tables, disagreement rows highlighted
      2. The engine's thinking — classification table, color-coded by action
      3. The outputs — daily Slack, weekly rollup, audit log sample

    No JavaScript, no external dependencies. Opens in any browser.
    """

    sf_df = pd.read_csv(SALESFORCE_CSV)
    mc_df = pd.read_csv(MISSION_CONTROL_CSV)

    with open(AUDIT_LOG, "r") as f:
        log = json.load(f)

    # Filter log to today's run only — dashboard shows the latest state.
    todays_entries = [e for e in log if e["run_date"] == RUN_DATE_STR]

    # Opportunity IDs that have a discrepancy this run — used for row highlighting.
    disagreement_ids = {e["opportunity_id"] for e in todays_entries}

    # Load the Slack files for rendering in Section 3.
    slack_daily_path = SLACK_DIR / f"slack_{RUN_DATE_STR}.txt"
    slack_weekly_path = SLACK_DIR / f"weekly_{RUN_DATE_STR}.txt"
    daily_slack = slack_daily_path.read_text() if slack_daily_path.exists() else ""
    weekly_slack = slack_weekly_path.read_text() if slack_weekly_path.exists() else ""

    # Convert the Slack text to displayable HTML — unwrap <url|label> into just the label
    # (bold), convert *bold* markers to <strong>, preserve line breaks.
    import re
    def _slack_to_html(text):
        text = re.sub(r"<([^|]+)\|([^>]+)>", r"\2", text)  # unwrap links to label text
        text = re.sub(r"\*([^*]+)\*", r"<strong>\1</strong>", text)  # bold
        text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<em>\1</em>", text)  # italic (only when not inside words)
        return text.replace("\n", "<br>")

    daily_html = _slack_to_html(daily_slack)
    weekly_html = _slack_to_html(weekly_slack)

    # Build the input tables.
    def _render_table(df, highlight_col, highlight_ids):
        headers = "".join(f"<th>{col}</th>" for col in df.columns)
        rows = []
        for _, row in df.iterrows():
            opp_id = str(row[highlight_col])
            # Match on either the raw or stripped (MC-prefixed) opportunity ID
            clean_id = opp_id.replace("MC-", "")
            row_class = ' class="flag-row"' if clean_id in highlight_ids else ""
            cells = "".join(f"<td>{row[col]}</td>" for col in df.columns)
            rows.append(f"<tr{row_class}>{cells}</tr>")
        return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"

    sf_table = _render_table(sf_df, "opportunity_id", disagreement_ids)
    mc_table = _render_table(mc_df, "mc_project_id", disagreement_ids)

    # Build the classification table.
    bucket_classes = {
        "owned_field": "action-correct",
        "shared_field": "action-correct",
        "missing_record": "action-flag",
        "contract_gap": "action-flag",
        "below_threshold": "action-hold",
    }

    classification_rows = []
    for e in todays_entries:
        row_class = bucket_classes.get(e["bucket"], "")
        classification_rows.append(f"""
            <tr class="{row_class}">
                <td><strong>{e['opportunity_id']}</strong></td>
                <td>{e['concept'] or '—'}</td>
                <td>{e['bucket'].replace('_', ' ')}</td>
                <td>{e['rule_applied']}</td>
                <td>{e['action'].replace('_', ' ')}</td>
                <td class="reason">{e['reason']}</td>
            </tr>
        """)
    classification_table = f"""
        <table>
            <thead>
                <tr>
                    <th>Opportunity</th>
                    <th>Field</th>
                    <th>Bucket</th>
                    <th>Rule</th>
                    <th>Action</th>
                    <th>Reason</th>
                </tr>
            </thead>
            <tbody>{''.join(classification_rows)}</tbody>
        </table>
    """

    # Build an audit log sample — the first 5 entries from today.
    log_sample = todays_entries[:5]
    log_rows = []
    for e in log_sample:
        log_rows.append(f"""
            <tr>
                <td>{e['run_timestamp'][:16].replace('T', ' ')}</td>
                <td>{e['opportunity_id']}</td>
                <td>{e['bucket'].replace('_', ' ')}</td>
                <td>{e['action'].replace('_', ' ')}</td>
                <td>{e['winning_system'] or '—'}</td>
            </tr>
        """)
    log_table = f"""
        <table class="log-sample">
            <thead>
                <tr>
                    <th>Timestamp (UTC)</th>
                    <th>Opportunity</th>
                    <th>Bucket</th>
                    <th>Action</th>
                    <th>Winner</th>
                </tr>
            </thead>
            <tbody>{''.join(log_rows)}</tbody>
        </table>
    """

    # Headline stats.
    corrections = sum(1 for e in todays_entries if e["action"] == "auto_correct")
    flags = sum(1 for e in todays_entries if e["action"] == "flag")
    held = sum(1 for e in todays_entries if e["action"] == "hold")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Deal Desk Reconciliation — {RUN_DATE_STR}</title>
<style>
  :root {{
    --bg: #fafafa;
    --panel: #ffffff;
    --text: #1a1a1a;
    --muted: #6b7280;
    --border: #e5e7eb;
    --flag: #fef3c7;
    --flag-border: #f59e0b;
    --correct: #dbeafe;
    --correct-border: #3b82f6;
    --hold: #f3f4f6;
    --hold-border: #9ca3af;
    --accent: #0f172a;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 2rem 1rem;
    line-height: 1.5;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; }}
  header {{ margin-bottom: 2.5rem; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 0.3rem 0; }}
  .subtitle {{ color: var(--muted); font-size: 0.95rem; }}
  .stats {{
    display: flex; gap: 2rem; margin-top: 1.25rem;
    padding: 1rem 1.25rem; background: var(--panel);
    border: 1px solid var(--border); border-radius: 6px;
  }}
  .stat-num {{ font-size: 1.5rem; font-weight: 600; }}
  .stat-label {{ font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }}
  section {{ margin-bottom: 3rem; }}
  h2 {{ font-size: 1.1rem; margin: 0 0 0.4rem 0; }}
  .section-desc {{ color: var(--muted); font-size: 0.9rem; margin: 0 0 1rem 0; }}
  .tables-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
  @media (max-width: 800px) {{ .tables-row {{ grid-template-columns: 1fr; }} }}
  .panel {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; overflow: hidden;
  }}
  .panel-head {{
    padding: 0.75rem 1rem; border-bottom: 1px solid var(--border);
    font-weight: 600; font-size: 0.9rem;
  }}
  .panel-body {{ padding: 1rem; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th, td {{ padding: 0.5rem 0.6rem; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }}
  th {{ font-weight: 600; color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; }}
  tr.flag-row td {{ background: var(--flag); border-left: 3px solid var(--flag-border); }}
  tr.flag-row td:first-child {{ font-weight: 600; }}
  tr.action-correct {{ background: var(--correct); }}
  tr.action-correct td {{ border-left: 3px solid var(--correct-border); }}
  tr.action-correct td:not(:first-child) {{ border-left: none; }}
  tr.action-flag {{ background: var(--flag); }}
  tr.action-flag td {{ border-left: 3px solid var(--flag-border); }}
  tr.action-flag td:not(:first-child) {{ border-left: none; }}
  tr.action-hold {{ background: var(--hold); }}
  tr.action-hold td {{ border-left: 3px solid var(--hold-border); }}
  tr.action-hold td:not(:first-child) {{ border-left: none; }}
  .reason {{ color: var(--muted); font-style: italic; }}
  .outputs-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
  @media (max-width: 800px) {{ .outputs-row {{ grid-template-columns: 1fr; }} }}
  .slack-render {{
    font-family: -apple-system, sans-serif;
    font-size: 0.88rem; padding: 1rem; line-height: 1.6;
  }}
  .log-sample {{ font-family: "SF Mono", Menlo, monospace; font-size: 0.78rem; }}
  footer {{
    margin-top: 3rem; padding-top: 1.5rem;
    border-top: 1px solid var(--border);
    color: var(--muted); font-size: 0.85rem;
  }}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>Deal Desk Reconciliation</h1>
  <div class="subtitle">Run of {RUN_DATE_STR} · showing today's state</div>
  <div class="stats">
    <div><div class="stat-num">{corrections}</div><div class="stat-label">Auto-corrections</div></div>
    <div><div class="stat-num">{flags}</div><div class="stat-label">Flagged for action</div></div>
    <div><div class="stat-num">{held}</div><div class="stat-label">Held below threshold</div></div>
    <div><div class="stat-num">{len(todays_entries)}</div><div class="stat-label">Total discrepancies</div></div>
  </div>
</header>

<section>
  <h2>1. What the system sees</h2>
  <p class="section-desc">Two systems, same opportunities, different vocabularies. Neither pushes to the other. Rows with discrepancies are highlighted.</p>
  <div class="tables-row">
    <div class="panel">
      <div class="panel-head">Salesforce export</div>
      <div class="panel-body">{sf_table}</div>
    </div>
    <div class="panel">
      <div class="panel-head">Mission Control export</div>
      <div class="panel-body">{mc_table}</div>
    </div>
  </div>
</section>

<section>
  <h2>2. How each discrepancy classified</h2>
  <p class="section-desc">The engine classifies, logs, and either acts or flags. Nothing happens that the SOP didn't explicitly authorize.</p>
  <div class="panel">
    <div class="panel-body">{classification_table}</div>
  </div>
</section>

<section>
  <h2>3. What each reader sees</h2>
  <p class="section-desc">Three outputs, three readers, three cadences — all from the same source.</p>
  <div class="outputs-row">
    <div class="panel">
      <div class="panel-head">Daily Slack post</div>
      <div class="panel-body slack-render">{daily_html}</div>
    </div>
    <div class="panel">
      <div class="panel-head">Weekly rollup</div>
      <div class="panel-body slack-render">{weekly_html}</div>
    </div>
  </div>
  <div class="panel" style="margin-top: 1rem;">
    <div class="panel-head">Audit log (sample)</div>
    <div class="panel-body">{log_table}</div>
  </div>
</section>

<footer>
  This page is a demo artifact. In production, each surface lives where its reader already works — Slack for the team, Salesforce for the resolution path, the audit log for anyone asking "when did this change and why." Nothing new to adopt.
</footer>

</div>
</body>
</html>
"""

    with open(DASHBOARD_HTML, "w") as f:
        f.write(html)

    return {
        "dashboard_path": str(DASHBOARD_HTML),
    }
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