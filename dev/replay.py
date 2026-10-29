"""Replay recorded failures without changing upstream tests or qualification results."""
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

spec = importlib.util.spec_from_file_location('dev_pipeline', Path(__file__).with_name('pipeline.py'))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def failure_nodes(evidence):
    failed = {e['nodeid'] for e in evidence['events'] if e['outcome'] == 'failed'}
    assert failed <= set(evidence['inventory']), 'Failure outside collected inventory'
    return [n for n in evidence['inventory'] if n in failed]


def plan():
    tag = os.environ['INPUT_REPORT']
    assert tag.startswith('dev-matrix-')
    report_release = p.release(tag)
    asset = next(a for a in report_release['assets'] if a['name'] == 'qualification.json')
    report = p.asset_json(report_release, 'qualification.json')
    cells = []
    for cell in report['cells']:
        nodes, evidence_sources = [], []
        for module in cell.get('suite', {}).get('modules', []):
            if not module.get('counts', {}).get('failed') and not module.get('failures'):
                continue
            archive = p.WORK / (p.identity(module['evidence_url']) + '.tar.gz')
            p.base.download(module['evidence_url'], archive, module['evidence_sha256'])
            with tarfile.open(archive) as tar:
                evidence = json.load(tar.extractfile('evidence.json'))
            nodes.extend(failure_nodes(evidence))
            evidence_sources.append({k: module[k] for k in ('module', 'evidence_url', 'evidence_sha256')})
        if not nodes:
            continue
        assert len(nodes) == len(set(nodes))
        arch = cell['base']['environment']['arch']
        assert arch in ('amd64', 'arm64')
        assert cell['image'] == 'ghcr.io/compiler-team-ru/pyasc-sim-dev@' + cell['digest']
        cells.append({k: cell[k] for k in ('id', 'key', 'image')} | {
            'source_sha': cell['base']['source_sha'], 'nodes': nodes, 'evidence': evidence_sources,
            'runner': 'ubuntu-24.04-arm' if arch == 'arm64' else 'ubuntu-24.04'})
    p.save(p.WORK / 'replay-plan.json', {'report': tag, 'report_digest': asset['digest'], 'cells': cells})
    p.base.output('matrix', json.dumps({'include': [{'key': c['key'], 'runner': c['runner']} for c in cells]}))
    p.base.output('count', len(cells))
    print(json.dumps({'cells': len(cells), 'recorded_failure_nodes': sum(len(c['nodes']) for c in cells)}))


def run():
    plan = json.loads((p.WORK / 'replay-plan.json').read_text())
    cell = next(c for c in plan['cells'] if c['key'] == os.environ['CELL_KEY'])
    result = {**cell, 'status': 'incomplete', 'scope': 'Diagnostic replay only; not full-suite qualification'}
    try:
        p.base.run('docker', 'pull', cell['image'])
        labels = json.loads(subprocess.check_output(['docker', 'image', 'inspect', cell['image'], '--format', '{{json .Config.Labels}}']))
        assert labels['io.pyasc.dev.id'] == cell['id']
        assert labels['org.opencontainers.image.revision'] == cell['source_sha']
        assert p.container(cell['image'], p.WORK / 'environment', ['python3', '-I', '/dev-check/check.py']) == 0
        result['source_archive_sha256'] = p.source_tree(cell['source_sha'])
        folder = p.WORK / 'replay'
        rc = p.container(cell['image'], folder, p.pytest_command(['/source/' + n for n in cell['nodes']]),
                         source=True, timeout=3600)
        result['container'] = json.loads((folder / 'container-state.json').read_text())
        evidence = json.loads((folder / 'evidence.json').read_text())
        result['summary'] = p.summarize(evidence, cell['nodes'])
        result['status'] = 'passed' if rc == 0 and result['summary']['suite_status'] == 'passed' else 'failed'
        assert result['status'] == 'passed', 'Recorded failures reproduced; inspect evidence'
    except Exception as error:
        result['reason'] = str(error)
        raise
    finally:
        p.save(p.WORK / 'replay-result.json', result)


def publish():
    plan = json.loads((p.WORK / 'replay-plan.json').read_text())
    cells, archives = [], []
    for cell in plan['cells']:
        folder = Path('results') / cell['key']
        result = folder / 'replay-result.json'
        cells.append(json.loads(result.read_text()) if result.exists() else {**cell, 'status': 'incomplete'})
        archive = p.WORK / (cell['key'] + '.tar.gz')
        with tarfile.open(archive, 'w:gz') as tar:
            for f in sorted(folder.rglob('*')):
                if f.is_file():
                    tar.add(f, arcname=str(f.relative_to(folder)))
        archives.append(str(archive))
    report = {**plan, 'cells': cells, 'scope': 'Diagnostic replay only; does not replace full-suite qualification'}
    target = p.WORK / 'replay-report.json'
    p.save(target, report)
    p.base.run('gh', 'release', 'create', 'dev-diagnosis-' + os.environ['GITHUB_RUN_ID'], str(target), *archives,
               '--repo', p.REPO, '--target', os.environ['GITHUB_SHA'], '--prerelease', '--latest=false',
               '--title', 'Recorded simulator failure replay', '--notes', report['scope'])
    print(json.dumps(dict(Counter(c['status'] for c in cells))))
    assert all(c['status'] == 'passed' for c in cells), 'Diagnosis remains incomplete or reproduced failures'


if __name__ == '__main__':
    p.WORK.mkdir(exist_ok=True)
    {'plan': plan, 'run': run, 'publish': publish}[sys.argv[1]]()
