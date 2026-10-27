"""Qualification progress and interruption evidence; never convert blockers to passes."""
from collections import Counter
import hashlib
import json
import urllib.request

# Reviewed compatibility: same immutable image, complete module inventory, Model /
# Ascend950PR_9599, 2 CPUs, 12 GiB, assertions and upstream markers unchanged.
# Only COMPLETE evidence can be imported. Remove these if execution semantics change.
COMPATIBLE_RECIPES = (
    'f731ab20ccbd7abb1169fb7a0d37d84330cbb36558b79082d39c1fbc6e63d9ce',
    'b245baa902eaa59889884850e7664757d027a821339bc089d1d50387458b88d5',
)


def asset_json(release, name):
    asset = next(a for a in release['assets'] if a['name'] == name)
    # Mutable release assets can otherwise resolve to stale CDN redirects.
    digest = asset.get('digest')
    url = asset['browser_download_url'] + '?asset=' + str(asset['id'])
    with urllib.request.urlopen(url, timeout=60) as response:
        raw = response.read()
    if digest:
        assert digest == 'sha256:' + hashlib.sha256(raw).hexdigest(), 'Release asset checksum mismatch'
    return json.loads(raw)


def interruption(returncode, state, budget_limited=False):
    if state.get('OOMKilled'):
        return 'oom', 'Docker reports that the container was OOM-killed', False
    if returncode == 124:
        return ('budget' if budget_limited else 'timeout',
                'Qualification batch budget exhausted' if budget_limited else 'Module exceeded the three-hour limit; diagnosis required',
                budget_limited)
    return 'interrupted', f'Incomplete execution: exit {returncode}; cause is not diagnosed', returncode not in (137, -9)


def partial_cases(evidence, nodes):
    result = []
    events = evidence.get('events', [])
    for node in nodes:
        phases = [e for e in events if e['nodeid'] == node]
        completed = any(e['phase'] == 'teardown' for e in phases)
        failure = next((e for e in phases if e['outcome'] == 'failed'), None)
        skip = next((e for e in phases if e['outcome'] == 'skipped'), None)
        if failure:
            status, outcome = 'failed', 'xpass' if '[XPASS(strict)]' in failure.get('detail', '') else 'failed'
        elif completed and skip:
            status, outcome = 'not-run', 'xfail' if skip.get('xfail') else 'skipped'
        elif completed:
            status, outcome = 'passed', 'passed'
        elif phases or node == evidence.get('active_node'):
            status, outcome = 'blocked', 'interrupted'
        else:
            status, outcome = 'not-run', 'not-run'
        result.append({'nodeid': node, 'status': status, 'outcome': outcome, 'completed': completed})
    return result


def aggregate(inventory, modules):
    counts = Counter()
    for module in modules:
        counts.update(module.get('counts', {}))
    complete = all(m['status'] == 'complete' for m in modules)
    return {'inventory': inventory, 'modules': modules, 'collected': len(inventory),
            'executed': sum(m.get('executed', 0) for m in modules), 'counts': dict(counts),
            'complete': complete, 'retryable': any(m['status'] != 'complete' and m.get('retryable', True) for m in modules),
            'publishable': complete and all(m.get('publishable', False) for m in modules),
            'suite_status': ('failures' if any(m.get('failures') for m in modules) else 'passed') if complete else 'incomplete'}


def cell_status(suite):
    if suite.get('complete', suite['suite_status'] in ('passed', 'failures')):
        return 'complete'
    return 'incomplete' if suite.get('retryable', True) else 'blocked'


def module_order(module):
    # Cheap IR checks first, resource-heavy target kernels last; no tests omitted.
    parts = module.split('/test/', 1)[-1].split('/')
    return ({'unit': 0, 'operations': 1, 'kernels': 2, 'target': 3}.get(parts[0], 4), module)
