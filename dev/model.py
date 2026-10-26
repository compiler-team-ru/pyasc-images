"""Overlay identities and conservative publication gates."""
import hashlib
import json
from collections import Counter
from pathlib import Path


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def recipe(root):
    # Publication policy and test scheduling do not change installed image contents.
    return identity({name: hashlib.sha256((root / 'dev' / name).read_bytes()).hexdigest()
                     for name in ('Dockerfile', 'requirements.lock', 'torch.lock.json')})


def qualification_recipe(root):
    names = ('check.py', 'evidence.py', 'pipeline.py', 'model.py')
    return identity({name: hashlib.sha256((root / 'dev' / name).read_bytes()).hexdigest() for name in names})


def expand(base, recipe_hash):
    assert len(base['cells']) == 32 and len({c['key'] for c in base['cells']}) == 32
    assert all(c['status'] in ('pass', 'incompatible') for c in base['cells'])
    cells = []
    for parent in base['cells']:
        assert parent['source_sha'] == base['source_sha']
        c = {'key': parent['key'], 'base': parent, 'recipe': recipe_hash,
             'id': identity({'base': parent.get('digest', parent['id']), 'recipe': recipe_hash})}
        if parent['status'] == 'pass':
            assert parent['image'] == 'ghcr.io/compiler-team-ru/pyasc-sim@' + parent['digest']
            e = parent['environment']
            if e['python'] not in ('3.10', '3.11', '3.12'):
                c['incompatibility'] = ('The development dependency lock supports Python 3.10–3.12 only. '
                                        'This base image passed; the overlay has no locked CPU Torch wheel for Python ' + e['python'] + '.')
            c['tag'] = (f'ghcr.io/compiler-team-ru/pyasc-sim-dev:v{parent["version"]}-cann{e["cann"]}'
                        f'-u{e["ubuntu"]}-py{e["python"]}-r{recipe_hash[:12]}-b{base["recipe"][:12]}-{e["arch"]}')
        cells.append(c)
    return cells


def pending(cells, previous, selected=''):
    keys = set(filter(None, selected.split(',')))
    assert not keys or keys <= {c['key'] for c in cells}, 'Unknown cell selection'
    done = {c['id'] for c in previous if c['status'] == 'published'}
    return [c for c in cells if c['base']['status'] == 'pass' and 'incompatibility' not in c
            and c['id'] not in done and (not keys or c['key'] in keys)]


def summarize(evidence, expected, diagnoses=()):
    assert evidence['inventory'] == expected, 'Collected inventory changed'
    events = evidence['events']
    assert not any(e['phase'] == 'collection' for e in events), 'Collection failed'
    completed = {e['nodeid'] for e in events if e['phase'] == 'teardown'}
    assert completed == set(expected), 'Incomplete test execution'
    assert evidence['exitstatus'] in (0, 1), 'Interrupted test execution'
    counts = Counter()
    failures = []
    for node in expected:
        phases = [e for e in events if e['nodeid'] == node]
        failed = [e for e in phases if e['outcome'] == 'failed']
        skip = next((e for e in phases if e['outcome'] == 'skipped'), None)
        call = next((e for e in phases if e['phase'] == 'call'), {})
        outcome = 'failed' if failed else ('xfail' if skip and skip.get('xfail') else 'skipped' if skip else
                                        'xpass' if call.get('xfail') else 'passed')
        if failed and any('[XPASS(strict)]' in e['detail'] for e in failed):
            outcome = 'xpass'
        counts[outcome] += 1
        for event in failed:
            detail_hash = hashlib.sha256(event['detail'].encode()).hexdigest()
            diagnosis = next((d for d in diagnoses if d['nodeid'] == node and d['detail_sha256'] == detail_hash), None)
            # An exact reviewed diagnostic is required; never infer safety from pytest's exit code.
            failures.append({**event, 'detail_sha256': detail_hash, 'diagnosis': diagnosis})
    blocked = any(not f['diagnosis'] or f['diagnosis']['category'] not in ('compiler', 'numerical', 'platform') for f in failures)
    return {'counts': dict(counts), 'failures': failures, 'publishable': not blocked,
            'suite_status': 'failures' if failures else 'passed', 'collected': len(expected), 'executed': len(completed)}


def pair_tag(pair):
    assert len(pair) == 2 and all(c['status'] == 'published' for c in pair)
    assert {c['base']['environment']['arch'] for c in pair} == {'amd64', 'arm64'}
    for key in ('version', 'source_sha', 'recipe'):
        assert pair[0]['base'][key] == pair[1]['base'][key]
    assert pair[0]['recipe'] == pair[1]['recipe']
    for key in ('ubuntu', 'cann', 'python', 'simulator'):
        assert pair[0]['base']['environment'][key] == pair[1]['base']['environment'][key]
    assert pair[0]['tag'].rsplit('-', 1)[0] == pair[1]['tag'].rsplit('-', 1)[0]
    return pair[0]['tag'].rsplit('-', 1)[0]
