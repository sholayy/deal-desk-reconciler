"""
resolve.py — attach a resolution to a previously observed incident.

The audit log captures what the engine saw. This script captures what the
human did about it afterward: the confirmed root cause, the structural fix
that was applied, and the team accountable for the fix. Over time, the
weekly rollup compares recurring incidents against prior resolutions and
reports whether a stated fix actually worked. That is the "verify prevention"
stage of the operating model turned into stored evidence.

Usage:
    python3 resolve.py OPP-012 \\
        --cause "Handoff Flow trigger did not fire for API-driven approval" \\
        --fix "Updated Flow trigger to listen for API as well as UI events" \\
        --team "Deal Desk Automation"

Options:
    OPP_ID (positional)      Opportunity ID to resolve. Required.
    --bucket                 Filter to a specific bucket if multiple entries exist
                             for this opportunity on the same run.
    --concept                Filter to a specific concept (field name) same way.
    --cause                  Confirmed root cause (free text). Required.
    --fix                    Corrective action applied (free text). Required.
    --team                   Team accountable for the fix (free text). Required.
    --resolved-by            Email of the human who resolved. Defaults to the
                             SOLA_EMAIL env var or 'ops@example.com'.
    --log-path               Path to the audit log. Defaults to output/reconciliation_log.json.

Behavior:
    Finds the most recent flagged incident matching OPP_ID and optional
    filters. Requires a specific bucket/concept when multiple incidents match;
    updates the resolution metadata for only the selected incident.
    The original observation is preserved.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from dateutil import parser as date_parser
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Append a resolution record to an audit log entry."
    )
    parser.add_argument("opp_id", help="Opportunity ID to resolve, e.g. OPP-012.")
    parser.add_argument("--bucket", help="Filter to a specific bucket.")
    parser.add_argument("--concept", help="Filter to a specific concept.")
    parser.add_argument("--cause", required=True, help="Confirmed root cause.")
    parser.add_argument("--fix", required=True, help="Corrective action applied.")
    parser.add_argument("--team", required=True, help="Team accountable for the fix.")
    parser.add_argument(
        "--resolved-by",
        default=os.environ.get("SOLA_EMAIL", "ops@example.com"),
        help="Email of the human who resolved.",
    )
    parser.add_argument(
        "--resolved-at",
        default=None,
        help=(
            "ISO 8601 timestamp for when the resolution was applied. "
            "Defaults to the current wall-clock time (UTC). The value must "
            "be strictly after the original observation. Backdating is "
            "allowed when the engine runs against a historical simulated_now "
            "so the full detect-fix-recur loop can be demonstrated end to end."
        ),
    )
    parser.add_argument(
        "--log-path",
        default="output/reconciliation_log.json",
        help="Path to the audit log.",
    )
    args = parser.parse_args()

    log_path = Path(args.log_path)
    if not log_path.exists():
        print(f"Audit log not found at {log_path}. Nothing to resolve.", file=sys.stderr)
        sys.exit(1)

    with open(log_path, "r") as f:
        log = json.load(f)

    # Find candidate entries. Filter by opp_id first, then by bucket/concept
    # if provided. Keep the newest run_id so re-resolving the same incident
    # updates the most recent observation rather than an old one.
    candidates = [
        e for e in log
        if e.get("opportunity_id") == args.opp_id and e.get("action") == "flag"
    ]
    if args.bucket:
        candidates = [e for e in candidates if e.get("bucket") == args.bucket]
    if args.concept:
        candidates = [e for e in candidates if e.get("concept") == args.concept]

    if not candidates:
        print(
            f"No audit log entries found for opportunity {args.opp_id}"
            + (f" bucket={args.bucket}" if args.bucket else "")
            + (f" concept={args.concept}" if args.concept else "")
            + ".",
            file=sys.stderr,
        )
        sys.exit(1)

    # Select the latest recorded observation, breaking simulated timestamp
    # ties by audit-log append order. Avoid changing multiple independent
    # discrepancies on the same opportunity by accident.
    newest = max(enumerate(candidates), key=lambda x: (x[1].get("run_timestamp", ""), x[0]))[1]
    target_run_id = newest.get("run_id")
    active = [e for e in candidates if e.get("run_id") == target_run_id]
    identities = {(e.get("concept"), e.get("bucket")) for e in active}
    if len(identities) > 1:
        print("Multiple flagged incidents match. Specify --bucket and/or --concept.", file=sys.stderr)
        sys.exit(2)

    now = datetime.now(timezone.utc)
    observed_at = date_parser.parse(newest["run_timestamp"])
    if observed_at.tzinfo is None:
        print("Cannot resolve an observation whose timestamp lacks a timezone.", file=sys.stderr)
        sys.exit(2)

    # Pick the resolution timestamp. Default is wall-clock now; --resolved-at
    # overrides. Enforce that the resolution timestamp is strictly after the
    # original observation so chronology stays sound in both directions.
    if args.resolved_at:
        try:
            resolved_at = date_parser.parse(args.resolved_at)
        except (ValueError, TypeError, OverflowError):
            print(f"Could not parse --resolved-at: {args.resolved_at}", file=sys.stderr)
            sys.exit(2)
        if resolved_at.tzinfo is None:
            print("--resolved-at must include a timezone (e.g. 2026-10-27T00:00:00Z).", file=sys.stderr)
            sys.exit(2)
    else:
        resolved_at = now

    if resolved_at <= observed_at:
        print(
            "Resolution timestamp must be strictly after the original observation "
            f"(observed at {observed_at.isoformat()}).",
            file=sys.stderr,
        )
        sys.exit(2)

    resolution = {
        "resolved_at": resolved_at.isoformat(),
        "resolved_by": args.resolved_by,
        "root_cause": args.cause,
        "corrective_action": args.fix,
        "responsible_team": args.team,
    }

    # Apply the resolution to every entry in that run that matches, since one
    # incident can produce multiple entries (same opportunity, same bucket,
    # different concepts for example). Humans resolve the incident; the log
    # entries are the engine's view of it.
    updated = 0
    for entry in log:
        if (
            entry.get("opportunity_id") == args.opp_id
            and entry.get("run_id") == target_run_id
            and (args.bucket is None or entry.get("bucket") == args.bucket)
            and (args.concept is None or entry.get("concept") == args.concept)
            and entry.get("action") == "flag"
        ):
            entry["resolution"] = resolution
            updated += 1

    with open(log_path, "w") as f:
        json.dump(log, f, indent=2)

    print(f"Resolution recorded on {updated} audit log entries.")
    print(f"  Opportunity: {args.opp_id}")
    if args.bucket:
        print(f"  Bucket: {args.bucket}")
    if args.concept:
        print(f"  Concept: {args.concept}")
    print(f"  Run: {target_run_id}")
    print(f"  Root cause: {args.cause}")
    print(f"  Corrective action: {args.fix}")
    print(f"  Responsible team: {args.team}")
    print(f"  Resolved by: {args.resolved_by}")


if __name__ == "__main__":
    main()
