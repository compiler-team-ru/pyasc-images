"""Pytest evidence plugin: preserve every collected node and all phase results."""
import json
import os
from pathlib import Path

inventory = []
events = []
active_node = None


def checkpoint(exitstatus=None):
    target = Path(os.environ["DEV_EVIDENCE"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"inventory": inventory, "events": events,
                                     "active_node": active_node, "exitstatus": exitstatus}) + "\n")
    temporary.replace(target)


def pytest_runtest_logstart(nodeid, location):
    global active_node
    active_node = nodeid
    checkpoint()


def pytest_collection_finish(session):
    inventory.extend(item.nodeid for item in session.items)
    checkpoint()


def pytest_collectreport(report):
    if report.failed or report.skipped:
        events.append({'nodeid': report.nodeid, 'phase': 'collection', 'outcome': report.outcome,
                       'detail': str(report.longrepr)})


def pytest_runtest_logreport(report):
    events.append({'nodeid': report.nodeid, 'phase': report.when, 'outcome': report.outcome,
                   'xfail': getattr(report, 'wasxfail', None), 'detail': str(report.longrepr or ''),
                   'duration': report.duration})
    checkpoint()


def pytest_sessionfinish(session, exitstatus):
    checkpoint(int(exitstatus))
