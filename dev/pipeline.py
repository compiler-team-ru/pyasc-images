"""Build development overlays and qualify the complete, source-pinned AscTile suite."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'ci'))
import pipeline as base
from model import expand, identity, pair_tag, pending, recipe, summarize

REPO = base.REPO
WORK = ROOT / 'work'
SUITE = 'experimental/asctile/python/test'


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def release(tag):
    result = subprocess.run(['gh', 'api', f'repos/{REPO}/releases/tags/{tag}'], capture_output=True, text=True)
    if result.returncode:
        assert '404' in result.stderr, result.stderr
        return None
    return json.loads(result.stdout)


def ensure_release(tag):
    if not release(tag):
        base.run('gh', 'release', 'create', tag, '--repo', REPO, '--target', os.environ['GITHUB_SHA'],
                 '--title', tag, '--notes', 'Development image qualification. See dev-matrix.json; checkpoints are not publication approval.')


def plan():
    WORK.mkdir(exist_ok=True)
    releases = base.pages(f'repos/{REPO}/releases')
    wanted = os.environ.get('INPUT_REPORT', '')
    candidates = [r for r in releases if r['tag_name'].startswith('matrix-') and not r['draft']
                  and (not wanted or r['tag_name'] == wanted)]
    chosen = None
    for r in sorted(candidates, key=lambda x: x['published_at']):
        report = base.asset_json(r, 'matrix.json')
        if len(report['cells']) != 32 or any(c['status'] not in ('pass', 'incompatible') for c in report['cells']):
            continue
        rh = recipe(ROOT)
        cells = expand(report, rh)
        tag = 'dev-' + r['tag_name'] + '-' + rh[:12]
        prior_release = release(tag)
        prior = base.asset_json(prior_release, 'dev-matrix.json') if prior_release and any(a['name'] == 'dev-matrix.json' for a in prior_release['assets']) else {}
        todo = pending(cells, prior.get('cells', []), os.environ.get('INPUT_CELLS', ''))
        chosen = {'cells': cells, 'pending': todo, 'previous': prior.get('cells', []), 'tag': tag,
                  'base_report': r['tag_name'], 'base_report_sha256': identity(report), 'recipe': rh}
        if todo or not prior or any(m['status'] != 'pass' for m in prior.get('manifests', [])):
            break
    assert chosen, 'No complete base matrix publication matches selection'
    ensure_release(chosen['tag'])
    save(WORK / 'dev-plan.json', chosen)
    base.output('matrix', json.dumps({'include': [{'key': c['key'], 'runner': c['base']['runner']} for c in chosen['pending']]}))
    base.output('count', len(chosen['pending']))
    base.output('report', str(bool(chosen['pending'] or not prior or any(m['status'] != 'pass' for m in prior.get('manifests', [])))).lower())
    print(json.dumps({'pending': len(chosen['pending']), 'cells': len(chosen['cells']), 'base_report': chosen['base_report']}))


def container(image, directory, command, *, source=False, timeout=1800, log=None):
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    name = 'dev-' + identity(str(directory))[:16]
    args = ['timeout', '--kill-after=20s', str(timeout), 'docker', 'run', '--rm', '--name', name,
            '--network', 'none', '--cpus', '2', '--memory', '12g', '--ulimit', 'nofile=65536:65536',
            '-v', str(directory.resolve()) + ':/results', '-w', '/results', '-e', 'HOME=/results',
            '-e', 'PYASC_CACHE_DIR=/results/cache', '-e', 'PYASC_DUMP_PATH=/results/dumps', '-e', 'PYASC_DRY_RUN=0',
            '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'OMP_NUM_THREADS=1', '-e', 'MKL_NUM_THREADS=1',
            '-e', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1', '-e', 'DEV_EVIDENCE=/results/evidence.json',
            '-v', str(ROOT / 'dev') + ':/dev-check:ro']
    if source:
        args += ['-v', str(WORK / 'source') + ':/source:ro', '-e', 'PYTHONPATH=/dev-check']
    try:
        with open(log or directory / 'output.log', 'w') as stream:
            result = subprocess.run(args + [image] + command, stdout=stream, stderr=subprocess.STDOUT)
        return result.returncode
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def source_tree(sha):
    archive = WORK / 'source.tar.gz'
    digest = base.download(f'https://api.github.com/repos/compiler-team-ru/pyasc/tarball/{sha}', archive)
    target = WORK / 'source'
    target.mkdir(exist_ok=True)
    with tarfile.open(archive) as tar:
        for member in tar:
            parts = Path(member.name).parts[1:]
            if not parts or member.isdir():
                continue
            relative = Path(*parts)
            assert '..' not in parts and not relative.is_absolute()
            assert member.isfile(), 'Source archive contains non-regular entry'
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(tar.extractfile(member).read())
    return digest


def assemble(cell):
    image = cell['tag']
    existing = base.inspect_image(image)
    if existing:
        base.run('docker', 'pull', image.split(':')[0] + '@' + existing)
        return image.split(':')[0] + '@' + existing, existing
    context = WORK / 'context'
    context.mkdir(exist_ok=True)
    for name in ('Dockerfile', 'requirements.lock'):
        shutil.copyfile(ROOT / 'dev' / name, context / name)
    wheels = context / 'wheels'
    wheels.mkdir(exist_ok=True)
    parent = cell['base']
    e = parent['environment']
    arch = {'amd64': 'x86_64', 'arm64': 'aarch64'}[e['arch']]
    torch = json.loads((ROOT / 'dev/torch.lock.json').read_text())['cp' + e['python'].replace('.', '') + '-' + arch]
    name = urllib.parse.unquote(torch['url'].rsplit('/', 1)[1])
    base.download(torch['url'], wheels / name, torch['sha256'])
    (wheels / 'torch.sha256').write_text(torch['sha256'] + '  ' + name + '\n')
    # Resolve/download inside the exact native base interpreter, constrained by its installed dependencies.
    base.run('docker', 'run', '--rm', '-v', str(context) + ':/download', parent['image'], 'bash', '-c',
             'python3 -m pip freeze --all > /tmp/base.txt && python3 -m pip download --only-binary=:all: '
             '--require-hashes --no-deps -c /tmp/base.txt -r /download/requirements.lock -d /download/wheels')
    image = 'pyasc-dev-candidate:local'
    base.run('docker', 'buildx', 'build', '--load', '-t', image, '--build-arg', 'BASE_IMAGE=' + parent['image'],
             '--build-arg', 'DEV_ID=' + cell['id'], str(context))
    return image, None


def pytest_command(paths, collect=False):
    return ['python3', '-m', 'pytest', '-p', 'evidence', '-c', '/source/pyproject.toml',
            '--backend', 'Model', '--platform', 'Ascend950PR_9599', '-ra',
            *(['--collect-only', '-q'] if collect else ['--junitxml=/results/junit.xml']), *paths]


def checkpoint_name(cell, module):
    return 'tests-' + cell['id'][:24] + '-' + identity(module)[:16] + '.tar.gz'


def suite(image, cell, tag):
    collect = WORK / 'collection'
    rc = container(image, collect, pytest_command(['/source/' + SUITE], True), source=True)
    assert rc == 0, 'Full-suite collection failed; see collection/output.log'
    inventory = json.loads((collect / 'evidence.json').read_text())['inventory']
    assert inventory and len(set(inventory)) == len(inventory), 'Empty or duplicate test inventory'
    modules = {}
    for node in inventory:
        modules.setdefault(node.split('::')[0], []).append(node)
    assets = {a['name']: a for a in release(tag)['assets']}
    summaries = []
    diagnoses = json.loads((ROOT / 'dev/diagnoses.json').read_text())
    for module, nodes in modules.items():
        name = checkpoint_name(cell, module)
        folder = WORK / 'shards' / identity(module)[:16]
        folder.mkdir(parents=True, exist_ok=True)
        archive = WORK / name
        if name in assets:
            base.download(assets[name]['browser_download_url'], archive)
            with tarfile.open(archive) as tar:
                for member in tar:
                    assert member.name in ('evidence.json', 'junit.xml', 'output.log', 'identity.json') and member.isfile()
                    (folder / member.name).write_bytes(tar.extractfile(member).read())
            assert json.loads((folder / 'identity.json').read_text()) == {'cell_id': cell['id'], 'module': module}
        else:
            path = module if module.startswith('/source/') else '/source/' + module
            rc = container(image, folder, pytest_command([path]), source=True, timeout=10800)
            assert rc in (0, 1), f'Incomplete module {module}: exit {rc}'
            assert (folder / 'evidence.json').exists(), f'Missing evidence: {module}'
            evidence = json.loads((folder / 'evidence.json').read_text())
            summarize(evidence, nodes, diagnoses)  # Verify complete before retaining a reusable checkpoint.
            save(folder / 'identity.json', {'cell_id': cell['id'], 'module': module})
            with tarfile.open(archive, 'w:gz') as tar:
                for filename in ('evidence.json', 'junit.xml', 'output.log', 'identity.json'):
                    tar.add(folder / filename, arcname=filename)
            base.run('gh', 'release', 'upload', tag, str(archive), '--repo', REPO)
        summary = summarize(json.loads((folder / 'evidence.json').read_text()), nodes, diagnoses)
        summaries.append({'module': module, **summary, 'evidence_url': f'https://github.com/{REPO}/releases/download/{tag}/{name}',
                          'evidence_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()})
        save(WORK / 'suite.json', {'inventory': inventory, 'modules': summaries})
        print(json.dumps({'module': module, 'counts': summary['counts'], 'publishable': summary['publishable']}), flush=True)
        # Compilation caches and dumps can be large. Keep only durable evidence.
        for cache in ('cache', 'dumps'):
            shutil.rmtree(folder / cache, ignore_errors=True)
    assert sum(s['executed'] for s in summaries) == len(inventory)
    return {'inventory': inventory, 'modules': summaries, 'collected': len(inventory),
            'executed': len(inventory), 'publishable': all(s['publishable'] for s in summaries),
            'suite_status': 'failures' if any(s['failures'] for s in summaries) else 'passed'}


def build():
    data = json.loads((WORK / 'dev-plan.json').read_text())
    cell = next(c for c in data['pending'] if c['key'] == os.environ['CELL_KEY'])
    result = {**cell, 'status': 'blocked', 'run_url': f'https://github.com/{REPO}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'}
    phase = 'assemble'
    try:
        image, existing = assemble(cell)
        labels = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image, '--format', '{{json .Config.Labels}}']))
        assert labels['io.pyasc.dev.id'] == cell['id'] and labels['io.pyasc.dev.base'] == cell['base']['image']
        phase = 'environment'
        assert container(image, WORK / 'environment', ['python3', '-I', '/dev-check/check.py']) == 0, 'Dev dependency check failed'
        result['environment_check'] = json.loads((WORK / 'environment/output.log').read_text().splitlines()[-1])
        phase = 'baseline'
        result['baseline_source'] = base.prepare(cell['base'])
        result['baseline'] = [base.verify_image(image, mode, WORK / ('baseline-' + mode)) for mode in ('identity', 'compile', 'model')]
        phase = 'suite'
        result['source_archive_sha256'] = source_tree(cell['base']['source_sha'])
        result['suite'] = suite(image, cell, data['tag'])
        assert result['suite']['publishable'], 'Undiagnosed test failures block publication; inspect suite evidence'
        phase = 'publish'
        if not existing:
            base.run('docker', 'tag', image, cell['tag'])
            base.run('docker', 'push', cell['tag'])
            existing = base.inspect_image(cell['tag'])
        assert existing
        result['digest'] = existing
        result['image'] = cell['tag'].split(':')[0] + '@' + existing
        result['public_registry_check'] = base.verify_public_digest(result['image'])
        base.run('docker', 'pull', result['image'])
        assert container(result['image'], WORK / 'public', ['python3', '-I', '/dev-check/check.py']) == 0
        result['public_environment_check'] = json.loads((WORK / 'public/output.log').read_text().splitlines()[-1])
        result['public_identity_check'] = base.verify_image(result['image'], 'identity', WORK / 'public-identity')
        result['status'] = 'published'
    except Exception as error:
        result.update(phase=phase, reason=str(error))
        if (WORK / 'suite.json').exists() and 'suite' not in result:
            result['partial_suite'] = json.loads((WORK / 'suite.json').read_text())
        raise
    finally:
        save(WORK / 'dev-result.json', result)


def finish():
    data = json.loads((WORK / 'dev-plan.json').read_text())
    results = {c['id']: c for c in data['previous']}
    for path in Path('results').glob('*/dev-result.json'):
        c = json.loads(path.read_text())
        assert c['id'] in {x['id'] for x in data['pending']}
        results[c['id']] = c
    cells = []
    for c in data['cells']:
        if c['base']['status'] == 'incompatible':
            cells.append({**c, 'status': 'incompatible', 'reason': c['base']['reason']})
        else:
            cells.append(results.get(c['id'], {**c, 'status': 'blocked', 'reason': 'Not yet qualified'}))
    groups = {}
    for c in cells:
        groups.setdefault(c['key'].rsplit('-', 1)[0], []).append(c)
    manifests = []
    for pair in groups.values():
        if len(pair) != 2 or any(c['status'] != 'published' for c in pair):
            continue
        tag = pair_tag(pair)
        try:
            digest = base.inspect_image(tag)
            if not digest:
                base.run('docker', 'buildx', 'imagetools', 'create', '-t', tag, *(c['image'] for c in pair))
                digest = base.inspect_image(tag)
            raw = json.loads(subprocess.check_output(['docker', 'buildx', 'imagetools', 'inspect', tag, '--raw']))
            children = {m['platform']['architecture']: m['digest'] for m in raw['manifests']}
            assert children == {c['base']['environment']['arch']: c['digest'] for c in pair}
            manifests.append({'tag': tag, 'digest': digest, 'children': children, 'status': 'pass',
                              'suite_status': {c['base']['environment']['arch']: c['suite']['suite_status'] for c in pair}})
        except Exception as error:
            manifests.append({'tag': tag, 'status': 'blocked', 'reason': str(error)})
    report = {k: data[k] for k in ('base_report', 'base_report_sha256', 'recipe')}
    report.update(schema_version=1, cells=cells, manifests=manifests)
    save('dev-matrix.json', report)
    base.run('gh', 'release', 'upload', data['tag'], 'dev-matrix.json', '--clobber', '--repo', REPO)
    if any(c['status'] == 'blocked' for c in cells) or any(m['status'] != 'pass' for m in manifests):
        raise SystemExit('Qualification incomplete; see durable dev-matrix.json and test evidence.')


if __name__ == '__main__':
    {'plan': plan, 'build': build, 'finish': finish}[sys.argv[1]]()
