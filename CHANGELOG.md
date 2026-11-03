# Improvements and validation

## Fixed

- Recurrence warnings now require that a documented resolution occurs **after** the original observation and **before** the next observation. This fixes the simulated October 28 run incorrectly showing a November 3 resolution as already failed.
- The weekly summary applies the same chronological rule before labeling any prior fix ineffective.
- Weekly run deduplication uses the last appended run for a given simulated date when timestamps tie.
- Incident identity is normalized across held-to-flagged transitions, avoiding false new/closed incidents.
- Weekly incidents absent from the current window are called **not observed this week**, not verified closed.
- Dashboard root-cause category is visibly labeled as a **possible category**, not a confirmed diagnosis. Owned-field mismatch no longer unconditionally implies missing upstream validation.
- `resolve.py` accepts only flagged incidents, guards against ambiguous simultaneous flags, and checks observation chronology. It continues to record the actual resolution UTC time.
- SOP, README, and runbook align on 1-hour field / 24-hour missing-record thresholds, proposed (not applied) corrections, and verification limits.

## Preserved

- Existing HTML dashboard layout and four KPI cards.
- Live Slack Incoming Webhook integration (not contacted during local testing).
- SOP-driven classification, CSV inputs, human-resolution CLI, and financial variance display.

## Validation

- Existing 11 classifier tests pass.
- Six new chronology and lifecycle regression tests pass.
- Full reconciliation against the supplied CSVs passes: 14 opportunities, 2 flags, 5 proposed corrections, 0 held.
- Actual Slack delivery was previously verified by the project owner; this package does not contain credentials and did not post a live notification.

## Limitations

- No real Salesforce/Mission Control write-back or authoritative contract version history.
- Root cause categories are tentative; only human investigation can confirm them.
- Resolution metadata updates entries in the JSON file, so the store is not tamper-proof.
- No full production integration or security validation.
- The demo uses a pinned October 28, 2026 run timestamp. A real November resolution cannot be used to claim a failed fix in that historical run.

## Installing

Replace the corresponding files in your repository, including the `tests/` and `data/` subfolders. Run `python3 -m pytest -q` and `python3 reconcile.py`. Before pushing the sample output to GitHub Pages, confirm the generated `output/dashboard.html` reflects your desired demo and no secrets are included. Set `SLACK_WEBHOOK_URL` only when you intentionally want to send real notifications.
