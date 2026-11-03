"""Regression tests: chronology, simulated runs, and incident lifecycle."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import reconcile


@pytest.fixture
def isolated_outputs(tmp_path, monkeypatch):
    output = tmp_path / 'output'
    slack = output / 'slack'
    slack.mkdir(parents=True)
    monkeypatch.setattr(reconcile, 'AUDIT_LOG', output / 'reconciliation_log.json')
    monkeypatch.setattr(reconcile, 'DASHBOARD_HTML', output / 'dashboard.html')
    monkeypatch.setattr(reconcile, 'SLACK_DIR', slack)
    monkeypatch.setattr(reconcile, 'SALESFORCE_CSV', Path(__file__).parents[1] / 'data' / 'salesforce_export.csv')
    monkeypatch.setattr(reconcile, 'MISSION_CONTROL_CSV', Path(__file__).parents[1] / 'data' / 'mission_control_export.csv')
    monkeypatch.setattr(reconcile, 'CONTRACT_YAML', Path(__file__).parents[1] / 'contract.yaml')
    monkeypatch.setattr(reconcile, '_SF_BASE_URL', None)
    monkeypatch.delenv('SLACK_WEBHOOK_URL', raising=False)
    return output


def record(when, run_id, *, resolution=None, opp='OPP-012', bucket='missing_record', concept=None):
    item = {
        'run_id': run_id, 'run_timestamp': when,
        'run_date': when[:10], 'opportunity_id': opp,
        'bucket': bucket, 'concept': concept, 'action': 'flag',
        'reason': 'Record present in salesforce only; missing from mission_control.',
        'root_cause_category': 'workflow_gap',
    }
    if resolution:
        item['resolution'] = {'resolved_at': resolution, 'responsible_team': 'Deal Desk Automation',
                              'corrective_action': 'Fixed trigger'}
    return item


def test_future_resolution_is_not_failed_fix(isolated_outputs, monkeypatch):
    current = '2026-10-28T13:30:00+00:00'
    monkeypatch.setattr(reconcile, 'RUN_TIMESTAMP', datetime.fromisoformat(current))
    monkeypatch.setattr(reconcile, 'RUN_DATE_STR', '2026-10-28')
    monkeypatch.setattr(reconcile, 'RUN_ID', 'current')
    data = [record('2026-10-27T13:30:00+00:00', 'prior', resolution='2026-11-03T11:00:00+00:00'),
            record(current, 'current')]
    reconcile.AUDIT_LOG.write_text(json.dumps(data))
    reconcile.emit_dashboard()
    html = reconcile.DASHBOARD_HTML.read_text()
    assert 'Still detected after recorded resolution' not in html


def test_real_post_resolution_repeat_is_flagged(isolated_outputs, monkeypatch):
    current = '2026-11-04T13:30:00+00:00'
    monkeypatch.setattr(reconcile, 'RUN_TIMESTAMP', datetime.fromisoformat(current))
    monkeypatch.setattr(reconcile, 'RUN_DATE_STR', '2026-11-04')
    monkeypatch.setattr(reconcile, 'RUN_ID', 'current')
    data = [record('2026-11-01T13:30:00+00:00', 'prior', resolution='2026-11-03T11:00:00+00:00'),
            record(current, 'current')]
    reconcile.AUDIT_LOG.write_text(json.dumps(data))
    reconcile.emit_dashboard()
    html = reconcile.DASHBOARD_HTML.read_text()
    assert 'Still detected after recorded resolution' in html
    assert 'Fixed trigger' in html


def test_resolution_prior_to_original_observation_is_invalid():
    original = record('2026-11-02T00:00:00+00:00', 'old', resolution='2026-11-01T00:00:00+00:00')
    assert reconcile._latest_prior_resolution([original], '2026-11-04T00:00:00+00:00') is None


def test_missing_record_keeps_incident_identity_after_hold():
    held = {'opportunity_id': 'OPP-012', 'concept': None, 'bucket': 'below_threshold'}
    flagged = {'opportunity_id': 'OPP-012', 'concept': None, 'bucket': 'missing_record'}
    assert reconcile._incident_key(held) == reconcile._incident_key(flagged)


def test_weekly_rollup_does_not_claim_future_fix_failed(isolated_outputs, monkeypatch):
    when = '2026-11-02T13:30:00+00:00'
    monkeypatch.setattr(reconcile, 'RUN_TIMESTAMP', datetime.fromisoformat(when))
    monkeypatch.setattr(reconcile, 'RUN_DATE_STR', '2026-11-02')
    data = [record('2026-10-22T13:30:00+00:00', 'prior', resolution='2026-11-03T11:00:00+00:00'),
            record(when, 'current')]
    reconcile.AUDIT_LOG.write_text(json.dumps(data))
    reconcile.emit_weekly_rollup()
    weekly = (isolated_outputs / 'slack' / 'weekly_2026-11-02.txt').read_text()
    assert 'STILL DETECTED AFTER RECORDED FIX' not in weekly
    assert 'root cause not yet captured' in weekly


def test_latest_same_timestamp_run_wins(isolated_outputs, monkeypatch):
    when = '2026-10-28T13:30:00+00:00'
    monkeypatch.setattr(reconcile, 'RUN_TIMESTAMP', datetime.fromisoformat(when))
    monkeypatch.setattr(reconcile, 'RUN_DATE_STR', '2026-10-28')
    data = [record(when, 'older', opp='OPP-012'), record(when, 'newer', opp='OPP-013')]
    reconcile.AUDIT_LOG.write_text(json.dumps(data))
    reconcile.emit_weekly_rollup()
    weekly = (isolated_outputs / 'slack' / 'weekly_2026-10-28.txt').read_text()
    assert '1 unique incidents this week' in weekly
    assert 'OPP-013' in weekly
    assert 'OPP-012' not in weekly
