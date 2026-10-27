"""Build development overlays and qualify the complete, source-pinned AscTile suite."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'ci'))
import pipeline as base
from model import expand, identity, pair_tag, pending, recipe, qualification_recipe, summarize

from qualification import COMPATIBLE_RECIPES, aggregate, asset_json, cell_status, interruption, module_order, partial_cases

RUN_STARTED = time.monotonic()
REPO = base.REPO
WORK = ROOT / 'work'
SUITE = 'experimental/asctile/python/test'
QUALIFY = os.environ.get('DEV_MODE') == 'qualify'


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
                 '--prerelease', '--latest=false', '--title', tag, '--notes', 'Development image qualification. See dev-matrix.json; checkpoints are not publication approval.')


def publish_qualification(data, cells):
    report = {k: data[k] for k in ('base_report', 'base_report_sha256', 'recipe', 'qualification_recipe')}
    report.update(schema_version=1, cells=cells,
                  scope='Full source-pinned AscTile suite; environment publication is independent.')
    name = 'qualification-' + data['qualification_recipe'][:16] + '.json'
    for filename in (name, 'qualification.json'):
        save(WORK / filename, report)
    base.run('gh', 'release', 'upload', data['tag'], str(WORK / name), str(WORK / 'qualification.json'),
             '--clobber', '--repo', REPO)


def plan():
    WORK.mkdir(exist_ok=True)
    releases = base.pages(f'repos/{REPO}/releases')
    wanted = os.environ.get('INPUT_REPORT', '')
    candidates = [r for r in releases if r['tag_name'].startswith('matrix-') and not r['draft']
                  and (not wanted or r['tag_name'] == wanted)]
    chosen = None
    def priority(candidate):
        if not QUALIFY or wanted:
            return ('', candidate['published_at'])
        dev_tag = 'dev-' + candidate['tag_name'] + '-' + recipe(ROOT)[:12]
        dev_release = next((r for r in releases if r['tag_name'] == dev_tag), {})
        qname = 'qualification-' + qualification_recipe(ROOT)[:16] + '.json'
        asset = next((a for a in dev_release.get('assets', []) if a['name'] == qname), {})
        return (asset.get('updated_at', ''), candidate['published_at'])
    for r in sorted(candidates, key=priority):
        report = asset_json(r, 'matrix.json')
        if len(report['cells']) != 32 or any(c['status'] not in ('pass', 'incompatible') for c in report['cells']):
            continue
        rh = recipe(ROOT)
        cells = expand(report, rh)
        tag = 'dev-' + r['tag_name'] + '-' + rh[:12]
        prior_release = release(tag)
        prior = asset_json(prior_release, 'dev-matrix.json') if prior_release and any(a['name'] == 'dev-matrix.json' for a in prior_release['assets']) else {}
        if QUALIFY:
            if not prior:
                continue
            published = {c['id']: c for c in prior['cells'] if c['status'] == 'published'}
            qr = qualification_recipe(ROOT)
            qname = 'qualification-' + qr[:16] + '.json'
            qprior = asset_json(prior_release, qname) if any(a['name'] == qname for a in prior_release['assets']) else {}
            complete = {c['id'] for c in qprior.get('cells', []) if c['status'] == 'complete' or
                        c.get('retryable') is False and os.environ.get('DEV_RETRY_BLOCKED') != 'true'}
            selected = set(filter(None, os.environ.get('INPUT_CELLS', '').split(',')))
            assert not selected or selected <= {c['key'] for c in cells}
            cells = [{**published.get(c['id'], c), 'qualification_recipe': qr} for c in cells]
            todo = [c for c in cells if c['id'] in published and c['id'] not in complete
                    and (not selected or c['key'] in selected)]
            prior = qprior
        else:
            todo = pending(cells, prior.get('cells', []), os.environ.get('INPUT_CELLS', ''))
        previous = {c['id']: c for c in prior.get('cells', [])}
        exclusions_changed = any('incompatibility' in c and
                                 (previous.get(c['id'], {}).get('status') != 'incompatible' or
                                  previous.get(c['id'], {}).get('reason') != c['incompatibility']) for c in cells)
        chosen = {'cells': cells, 'pending': todo, 'previous': prior.get('cells', []), 'tag': tag,
                  'base_report': r['tag_name'], 'base_report_sha256': identity(report), 'recipe': rh,
                  'mode': 'qualify' if QUALIFY else 'publish', 'qualification_recipe': qualification_recipe(ROOT)}
        refresh = bool(todo or not prior or exclusions_changed or any(m['status'] != 'pass' for m in prior.get('manifests', [])))
        if refresh:
            break
    if chosen is None and QUALIFY:
        save(WORK / 'dev-plan.json', {'mode': 'qualify', 'pending': [], 'cells': []})
        base.output('matrix', json.dumps({'include': []}))
        base.output('count', 0)
        base.output('report', 'false')
        print('Waiting for an environment-qualified development publication')
        return
    assert chosen, 'No complete base matrix publication matches selection'
    ensure_release(chosen['tag'])
    save(WORK / 'dev-plan.json', chosen)
    if QUALIFY and chosen['pending']:
        previous = {c['id']: c for c in chosen['previous']}
        initial = []
        for c in chosen['cells']:
            if c['base']['status'] == 'incompatible' or 'incompatibility' in c:
                initial.append({**c, 'status': 'incompatible', 'reason': c.get('incompatibility', c['base'].get('reason'))})
            else:
                initial.append(previous.get(c['id'], {**c, 'status': 'pending',
                                                     'reason': 'Full-suite execution has not completed for this qualification recipe.'}))
        publish_qualification(chosen, initial)
    base.output('matrix', json.dumps({'include': [{'key': c['key'], 'runner': c['base']['runner']} for c in chosen['pending']]}))
    base.output('count', len(chosen['pending']))
    base.output('report', str(refresh).lower())
    print(json.dumps({'pending': len(chosen['pending']), 'cells': len(chosen['cells']), 'base_report': chosen['base_report']}))


def container(image, directory, command, *, source=False, timeout=1800, log=None):
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    name = 'dev-' + identity(str(directory))[:16]
    args = ['timeout', '--kill-after=20s', str(timeout), 'docker', 'run', '--name', name,
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
        inspection = subprocess.run(['docker', 'inspect', name, '--format', '{{json .State}}'], capture_output=True, text=True)
        state = json.loads(inspection.stdout) if inspection.returncode == 0 else {}
        save(directory / 'container-state.json', {'returncode': result.returncode, 'timeout_seconds': timeout,
                                                'state': state, 'inspection_error': inspection.stderr if inspection.returncode else None})
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
    return ['python3', '-m', 'pytest', *paths, '-p', 'evidence', '-c', '/source/pyproject.toml',
            '--backend', 'Model', '--platform', 'Ascend950PR_9599', '-ra',
            *(['--collect-only', '-q'] if collect else ['--junitxml=/results/junit.xml'])]


def checkpoint_name(cell, module):
    return 'tests-' + cell['id'][:24] + '-' + identity(module)[:16] + '.tar.gz'


def collect(image):
    folder = WORK / 'collection'
    rc = container(image, folder, pytest_command(['/source/' + SUITE], True), source=True)
    assert rc == 0, 'Full-suite collection failed; see collection/output.log'
    evidence = json.loads((folder / 'evidence.json').read_text())
    assert not evidence['events'], 'Collection skipped or failed modules; inspect collection evidence'
    inventory = evidence['inventory']
    assert inventory and len(set(inventory)) == len(inventory), 'Empty or duplicate test inventory'
    return inventory


def suite(image, cell, tag):
    tag = 'dev-tests-' + cell['id'] + '-' + cell['qualification_recipe'][:12]
    ensure_release(tag)
    inventory = collect(image)
    modules = {}
    for node in inventory:
        modules.setdefault(node.split('::')[0], []).append(node)
    current = release(tag)
    sources = [(cell['qualification_recipe'], tag, current)]
    for compatible in COMPATIBLE_RECIPES:
        old_tag = 'dev-tests-' + cell['id'] + '-' + compatible[:12]
        old = release(old_tag)
        if old:
            sources.append((compatible, old_tag, old))
    prior = asset_json(current, 'progress.json') if any(a['name'] == 'progress.json' for a in current['assets']) else {}
    if prior:
        assert prior['cell_id'] == cell['id'] and prior['qualification_recipe'] == cell['qualification_recipe']
        assert prior['suite']['inventory'] == inventory
    previous = {m['module']: m for m in prior.get('suite', {}).get('modules', [])}
    summaries = {m: {'module': m, 'status': 'not-run', 'collected': len(nodes), 'executed': 0}
                 for m, nodes in modules.items()}
    diagnoses = json.loads((ROOT / 'dev/diagnoses.json').read_text())
    budget = int(os.environ.get('DEV_BUDGET_MINUTES') or '240') * 60
    assert 60 <= budget <= 18000
    deadline = RUN_STARTED + budget
    maximum = int(os.environ.get('DEV_MAX_MODULES') or '0')
    assert maximum >= 0
    attempted = 0
    last_upload = 0.0

    def checkpoint(force=False):
        nonlocal last_upload
        result = aggregate(inventory, list(summaries.values()))
        save(WORK / 'suite.json', result)
        save(WORK / 'progress.json', {'cell_id': cell['id'], 'qualification_recipe': cell['qualification_recipe'],
                                     'run_url': f'https://github.com/{REPO}/actions/runs/{os.environ["GITHUB_RUN_ID"]}',
                                     'suite': result})
        if force or time.monotonic() - last_upload >= 300:
            base.run('gh', 'release', 'upload', tag, str(WORK / 'progress.json'), '--clobber', '--repo', REPO)
            last_upload = time.monotonic()
        return result

    # Restore every reusable module before allocating time to unfinished tests.
    remaining = []
    for module in sorted(modules, key=module_order):
        nodes = modules[module]
        name = checkpoint_name(cell, module)
        found = next(((qr, source_tag, a) for qr, source_tag, r in sources for a in r['assets'] if a['name'] == name), None)
        folder = WORK / 'shards' / identity(module)[:16]
        folder.mkdir(parents=True, exist_ok=True)
        if found:
            qr, source_tag, asset = found
            archive = WORK / name
            expected_hash = (asset.get('digest') or '').removeprefix('sha256:') or None
            base.download(asset['browser_download_url'] + '?asset=' + str(asset['id']), archive, expected_hash)
            with tarfile.open(archive) as tar:
                for member in tar:
                    assert member.name in ('evidence.json', 'junit.xml', 'output.log', 'identity.json', 'container-state.json') and member.isfile()
                    (folder / member.name).write_bytes(tar.extractfile(member).read())
            assert json.loads((folder / 'identity.json').read_text()) == {'cell_id': cell['id'], 'module': module, 'qualification_recipe': qr}
            summary = summarize(json.loads((folder / 'evidence.json').read_text()), nodes, diagnoses)
            summaries[module] = {'module': module, 'status': 'complete', **summary,
                                 'evidence_recipe': qr, 'evidence_url': asset['browser_download_url'],
                                 'evidence_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
        elif previous.get(module, {}).get('retryable') is False and os.environ.get('DEV_RETRY_BLOCKED') != 'true':
            summaries[module] = previous[module]
        else:
            remaining.append(module)
    checkpoint(force=True)
    for module in remaining:
        seconds = int(deadline - time.monotonic() - 120)
        if seconds < 60 or maximum and attempted >= maximum:
            break
        nodes = modules[module]
        folder = WORK / 'shards' / identity(module)[:16]
        summaries[module].update(status='running', retryable=True)
        checkpoint()
        limit = min(10800, seconds)
        path = module if module.startswith('/source/') else '/source/' + module
        rc = container(image, folder, pytest_command([path]), source=True, timeout=limit)
        attempted += 1
        evidence = json.loads((folder / 'evidence.json').read_text()) if (folder / 'evidence.json').exists() else {}
        complete = False
        try:
            assert rc in (0, 1), f'Interrupted: exit {rc}'
            summary = summarize(evidence, nodes, diagnoses)
            complete = True
        except (AssertionError, KeyError) as error:
            state = json.loads((folder / 'container-state.json').read_text()).get('state', {})
            category, reason, retryable = interruption(rc, state, limit < 10800)
            if rc in (0, 1):
                category, reason = 'evidence', str(error)
            cases = partial_cases(evidence, nodes)
            summary = {'suite_status': 'incomplete', 'category': category, 'reason': reason,
                       'retryable': retryable, 'collected': len(nodes),
                       'executed': sum(c['completed'] for c in cases), 'cases': cases,
                       'counts': dict(Counter(c['outcome'] for c in cases if c['completed'])),
                       'container': json.loads((folder / 'container-state.json').read_text()), 'publishable': False}
        name = checkpoint_name(cell, module)
        if not complete:
            name = ('blocked-' + name.removeprefix('tests-').removesuffix('.tar.gz') + '-' +
                    os.environ['GITHUB_RUN_ID'] + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1') + '.tar.gz')
        archive = WORK / name
        save(folder / 'identity.json', {'cell_id': cell['id'], 'module': module, 'qualification_recipe': cell['qualification_recipe']})
        with tarfile.open(archive, 'w:gz') as tar:
            for filename in ('evidence.json', 'junit.xml', 'output.log', 'identity.json', 'container-state.json'):
                if (folder / filename).exists():
                    tar.add(folder / filename, arcname=filename)
        base.run('gh', 'release', 'upload', tag, str(archive), '--repo', REPO)
        summaries[module] = {'module': module, 'status': 'complete' if complete else 'blocked', **summary,
                             'evidence_recipe': cell['qualification_recipe'],
                             'evidence_url': f'https://github.com/{REPO}/releases/download/{tag}/{name}',
                             'evidence_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
        checkpoint(force=not complete)
        print(json.dumps({'module': module, 'status': summaries[module]['status'], 'counts': summary['counts']}), flush=True)
        for cache in ('cache', 'dumps'):
            shutil.rmtree(folder / cache, ignore_errors=True)
    return checkpoint(force=True)

def build():
    global RUN_STARTED
    RUN_STARTED = time.monotonic()
    data = json.loads((WORK / 'dev-plan.json').read_text())
    cell = next(c for c in data['pending'] if c['key'] == os.environ['CELL_KEY'])
    result = {**cell, 'status': 'blocked', 'run_url': f'https://github.com/{REPO}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'}
    if QUALIFY:
        result.pop('suite', None)
    phase = 'assemble'
    try:
        if QUALIFY:
            image, existing = cell['image'], cell['digest']
            base.run('docker', 'pull', image)
        else:
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
        if QUALIFY:
            result['suite'] = suite(image, cell, data['tag'])
            result['review_status'] = 'reviewed' if result['suite']['publishable'] else 'needs-review'
            result['status'] = cell_status(result['suite'])
            result['retryable'] = result['suite'].get('retryable', False)
            if result['status'] != 'complete':
                result.update(phase='suite', reason='Batch saved with unfinished or blocked modules; inspect suite.modules')
            return
        inventory = collect(image)
        result['suite'] = {'suite_status': 'pending', 'collected': len(inventory), 'executed': 0,
                           'inventory': inventory, 'status_at_publication': 'pending'}
        result['qualification_release'] = f'https://github.com/{REPO}/releases/tag/{data["tag"]}'
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
        result.update(status='incomplete' if QUALIFY else 'blocked', phase=phase, reason=str(error))
        if (WORK / 'suite.json').exists():
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
    if QUALIFY:
        for c in data['pending']:
            record = results.get(c['id'], {**c, 'status': 'incomplete', 'phase': 'runner',
                                          'reason': 'Runner interrupted before writing its result'})
            folder = Path('results') / ('result-' + c['key'])
            local = folder / 'suite.json'
            partial = json.loads(local.read_text()) if local.exists() else record.get('partial_suite')
            if partial is None and c['id'] not in results:
                checkpoint_release = release('dev-tests-' + c['id'] + '-' + c['qualification_recipe'][:12])
                if checkpoint_release and any(a['name'] == 'progress.json' for a in checkpoint_release['assets']):
                    progress = asset_json(checkpoint_release, 'progress.json')
                    assert progress['cell_id'] == c['id'] and progress['qualification_recipe'] == c['qualification_recipe']
                    partial = progress['suite']
            if partial:
                for module in partial['modules']:
                    module.setdefault('status', 'complete')
                    if module['status'] == 'running':
                        module.update(status='blocked', reason='Runner interrupted during this module', retryable=True)
                record['suite'] = aggregate(partial['inventory'], partial['modules'])
                record['status'] = cell_status(record['suite'])
                record['retryable'] = record['suite']['retryable']
                record.pop('partial_suite', None)
            results[c['id']] = record
    cells = []
    for c in data['cells']:
        if c['base']['status'] == 'incompatible' or 'incompatibility' in c:
            cells.append({**c, 'status': 'incompatible', 'reason': c.get('incompatibility', c['base'].get('reason'))})
        else:
            cells.append(results.get(c['id'], {**c, 'status': 'pending' if QUALIFY else 'blocked', 'reason': 'Not yet qualified'}))
    if QUALIFY:
        publish_qualification(data, cells)
        print(json.dumps({'qualification_states': dict(Counter(c['status'] for c in cells))}))
        if any(c['status'] == 'complete' and c['suite']['suite_status'] == 'failures' for c in cells):
            raise SystemExit('Completed suites contain test failures; environment publication is unaffected.')
        if any(c['status'] not in ('complete', 'incompatible') for c in cells):
            print('::notice::Qualification progress saved; the full suite remains incomplete. See qualification.json.')
        return
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
