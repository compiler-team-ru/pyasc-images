"""Pytest evidence plugin: preserve every collected node and all phase results."""
import json
import os
from pathlib import Path

inventory = []
events = []


def pytest_collection_finish(session):
    inventory.extend(item.nodeid for item in session.items)


def pytest_collectreport(report):
    if report.failed or report.skipped:
        events.append({'nodeid': report.nodeid, 'phase': 'collection', 'outcome': report.outcome,
                       'detail': str(report.longrepr)})


def pytest_runtest_logreport(report):
    events.append({'nodeid': report.nodeid, 'phase': report.when, 'outcome': report.outcome,
                   'xfail': getattr(report, 'wasxfail', None), 'detail': str(report.longrepr or ''),
                   'duration': report.duration})


def pytest_sessionfinish(session, exitstatus):
    Path(os.environ['DEV_EVIDENCE']).write_text(json.dumps({
        'inventory': inventory, 'events': events, 'exitstatus': int(exitstatus)}, indent=2) + '\n')
