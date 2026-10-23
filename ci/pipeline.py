"""Resolve durable publications, assemble a matrix, and retain verified results."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

from matrix import environments, expand, file_hash, identity, merge_pair, pending, recipe_hash

ROOT = Path(__file__).resolve().parent.parent
REPO = 'compiler-team-ru/pyasc-images'
WHEELS_REPO = 'compiler-team-ru/asctile'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def gh(path):
    return json.loads(subprocess.check_output(['gh', 'api', path], text=True))


def pages(path):
    result = []
    for page in range(1, 101):
        batch = gh(f'{path}?per_page=100&page={page}')
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise RuntimeError('Pagination limit exceeded')


def download(url, destination, checksum=None):
    request = urllib.request.Request(url, headers={'User-Agent': 'pyasc-images'})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=120) as response, Path(destination).open('wb') as output:
        for chunk in iter(lambda: response.read(1024 * 1024), b''):
            digest.update(chunk)
            output.write(chunk)
    if checksum:
        assert digest.hexdigest() == checksum, 'Download checksum mismatch'
    return digest.hexdigest()


def asset_json(release, name):
    asset = next(a for a in release['assets'] if a['name'] == name)
    with urllib.request.urlopen(asset['browser_download_url'], timeout=60) as response:
        return json.load(response)


def output(key, value):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write(key + '=' + str(value) + '\n')


def environment_recipe():
    return identity({name: file_hash(ROOT / name)
                     for name in ('Dockerfile', 'entrypoint.sh', 'requirements.lock')})


def prepare_environments():
    cells = environments(json.loads((ROOT / 'inputs.lock.json').read_text()), environment_recipe())
    selected = set(os.environ['INPUT_CELLS'].split(','))
    assert selected and selected <= {c['key'] for c in cells}, 'Select explicit environment cells'
    output('matrix', json.dumps({'include': [{'key': c['key'], 'runner': c['runner']} for c in cells if c['key'] in selected]}))


def build_environment():
    cells = environments(json.loads((ROOT / 'inputs.lock.json').read_text()), environment_recipe())
    cell = next(c for c in cells if c['key'] == os.environ['CELL_KEY'])
    print(ensure_environment(cell))


def plan():
    lock = json.loads((ROOT / 'inputs.lock.json').read_text())
    recipe = recipe_hash(ROOT)
    env_recipe = environment_recipe()
    image_releases = {r['tag_name']: r for r in pages(f'repos/{REPO}/releases') if not r['draft']}
    releases = [r for r in pages(f'repos/{WHEELS_REPO}/releases')
                if not r['draft'] and r['tag_name'].startswith('wheel-')
                and any(a['name'] == 'wheel-manifest.json' for a in r['assets'])]
    version = os.environ.get('INPUT_VERSION', '')
    selected = os.environ.get('INPUT_CELLS', '')
    candidates = []
    for release in sorted(releases, key=lambda r: r['published_at']):
        if version and release['tag_name'] != 'wheel-' + version:
            continue
        publication = asset_json(release, 'wheel-manifest.json')
        if publication.get('status') == 'complete':
            candidates.append((release, publication))
    if version:
        assert candidates, 'Requested wheel publication does not exist'
    else:
        bootstrap = next((r['published_at'] for r, p in candidates if p['source_sha'] == lock['bootstrap_source']), None)
        assert bootstrap, 'Waiting for the complete frozen-source wheel publication'
        candidates = [(r, p) for r, p in candidates if r['published_at'] >= bootstrap]
    chosen = None
    for release, publication in candidates:
        cells = expand(lock, publication, recipe, env_recipe)
        tag = 'matrix-' + publication['version'] + '-' + recipe[:16]
        prior = asset_json(image_releases[tag], 'matrix.json') if tag in image_releases else {}
        previous = prior.get('cells', [])
        todo = pending(cells, previous, selected)
        passed_keys = {r['key'] for r in previous if r['status'] == 'pass'}
        paired = sum(c['environment']['arch'] == 'amd64' and c['key'] in passed_keys
                     and c['key'].rsplit('-', 1)[0] + '-arm64' in passed_keys for c in cells)
        complete_manifests = sum(bool(m.get('digest')) and m.get('status', 'pass') == 'pass'
                                 for m in prior.get('manifests', []))
        needs_report = bool(todo or not prior or paired != complete_manifests)
        chosen = {'publication': publication, 'cells': cells, 'previous': previous,
                  'tag': tag, 'recipe': recipe, 'pending': todo, 'needs_report': needs_report}
        if needs_report:
            break
    assert chosen, 'No complete wheel publication'
    Path('work').mkdir(exist_ok=True)
    Path('work/plan.json').write_text(json.dumps(chosen, indent=2) + '\n')
    output('matrix', json.dumps({'include': [{'key': c['key'], 'runner': c['runner']} for c in chosen['pending']]}))
    output('count', len(chosen['pending']))
    output('tag', chosen['tag'])
    output('report', str(chosen['needs_report']).lower())
    print(json.dumps({'version': chosen['publication']['version'], 'pending': len(chosen['pending']), 'cells': len(chosen['cells'])}))


def prepare(cell):
    Path('cell.json').write_text(json.dumps(cell, indent=2) + '\n')
    wheel_dir = Path('wheel')
    wheel_dir.mkdir()
    wheel = cell['wheel']
    download(wheel['url'], wheel_dir / wheel['filename'], wheel['sha256'])
    (wheel_dir / 'SHA256SUMS').write_text(wheel['sha256'] + '  ' + wheel['filename'] + '\n')
    verification = Path('work/verification')
    verification.mkdir(parents=True)
    source_path = 'experimental/asctile/python/test/target/test_vadd.py'
    url = f'https://raw.githubusercontent.com/compiler-team-ru/pyasc/{cell["source_sha"]}/{source_path}'
    source_hash = download(url, verification / 'upstream-original.py')
    source = (verification / 'upstream-original.py').read_text()
    tree = ast.parse(source)
    kernel = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'add')
    # Keep the actual kernel and decorators byte-for-byte, without importing its Torch test harness.
    start = min([kernel.lineno] + [d.lineno for d in kernel.decorator_list])
    lines = source.splitlines(keepends=True)
    header = ''.join(lines[:next(i for i, line in enumerate(lines) if line.startswith('import '))])
    extracted = header + 'from asc.experimental import asctile\n\n' + ''.join(lines[start - 1:kernel.end_lineno])
    (verification / 'upstream_kernel.py').write_text(extracted)
    (verification / 'smoke.py').write_bytes((ROOT / 'ci/smoke.py').read_bytes())
    return {'url': url, 'file_sha256': source_hash,
            'extracted_sha256': hashlib.sha256(extracted.encode()).hexdigest(), 'function': 'add'}


def inspect_image(image):
    result = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', image, '--format', '{{json .Manifest}}'],
                            capture_output=True, text=True)
    if result.returncode:
        if re.search(r'not found|manifest unknown|404', result.stderr, re.I):
            return None
        # GHCR reports denied rather than 404 before a package's first push.
        # Only treat it as absent when the authenticated package API confirms 404.
        if re.search(r'denied|unauthorized', result.stderr, re.I):
            package = image.split('/')[-1].split(':')[0].split('@')[0]
            assert image.startswith('ghcr.io/compiler-team-ru/pyasc-')
            probe = subprocess.run(['gh', 'api', f'orgs/compiler-team-ru/packages/container/{package}'],
                                   capture_output=True, text=True)
            if probe.returncode and 'HTTP 404' in probe.stderr:
                return None
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)['digest']


def verify_public_digest(image):
    repository, digest = image.removeprefix('ghcr.io/').split('@')
    with urllib.request.urlopen('https://ghcr.io/token?service=ghcr.io&scope=repository:' + repository + ':pull', timeout=60) as response:
        token = json.load(response)['token']
    request = urllib.request.Request('https://ghcr.io/v2/' + repository + '/manifests/' + digest,
                                     headers={'Authorization': 'Bearer ' + token,
                                              'Accept': 'application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        assert 'sha256:' + hashlib.sha256(response.read()).hexdigest() == digest
    return {'anonymous_registry_access': True, 'digest': digest}


def verify_image(image, mode, directory):
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    name = 'pyasc-check-' + os.environ.get('GITHUB_RUN_ID', 'local') + '-' + mode
    try:
        run('timeout', '--kill-after=20s', '900s', 'docker', 'run', '--rm', '--name', name,
            '--network', 'none', '--cpus', '2', '--memory', '12g', '--ulimit', 'nofile=65536:65536',
            '-v', str(Path('work/verification').resolve()) + ':/verification:ro',
            '-v', str(directory.resolve()) + ':/results', '-w', '/results',
            '-e', 'HOME=/results', image, 'python3', '-I', '/verification/smoke.py', mode)
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return json.loads((directory / ('result-' + mode + '.json')).read_text())


def ensure_environment(cell):
    env = cell['environment']
    env_digest = inspect_image(cell['environment_image'])
    if not env_digest:
        args = {'UBUNTU_IMAGE': env['ubuntu_image'],
                'PYTHON_VERSION': env['python_source']['version'],
                'PYTHON_URL': env['python_source']['url'],
                'PYTHON_SHA256': env['python_source']['sha256'],
                'CANN_URL': env['cann_installer']['url'], 'CANN_SHA256': env['cann_installer']['sha256'],
                'CANN_INSTALL_ARGS': ' '.join(env['cann_installer']['install_args'])}
        command = ['docker', 'buildx', 'build', '--target', 'environment', '--push', '-t', cell['environment_image'],
                   '--platform', 'linux/' + env['arch'],
                   '--cache-from', 'type=registry,ref=ghcr.io/compiler-team-ru/pyasc-env:cache-' + cell['key'],
                   '--cache-to', 'type=registry,ref=ghcr.io/compiler-team-ru/pyasc-env:cache-' + cell['key'] + ',mode=max']
        for key, value in args.items():
            command += ['--build-arg', key + '=' + value]
        run(*command, '.')
        env_digest = inspect_image(cell['environment_image'])
    assert env_digest
    return env_digest


def build_cell():
    plan_data = json.loads(Path('work/plan.json').read_text())
    cell = next(c for c in plan_data['pending'] if c['key'] == os.environ['CELL_KEY'])
    result = {**cell, 'status': 'failed', 'run_url': f'https://github.com/{REPO}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'}
    phase = 'prepare'
    try:
        result['upstream_kernel'] = prepare(cell)
        env = cell['environment']
        phase = 'environment'
        env_digest = ensure_environment(cell)
        result['environment_digest'] = env_digest
        env_image = cell['environment_image'].split(':')[0] + '@' + env_digest
        existing = inspect_image(cell['tag'])
        phase = 'assemble'
        if existing:
            # Recover a previous push whose report was interrupted; never overwrite its tag.
            image = cell['tag'].split(':')[0] + '@' + existing
            run('docker', 'pull', image)
        else:
            image = 'pyasc-candidate:local'
            run('docker', 'buildx', 'build', '--target', 'simulator', '--load', '-t', image,
                '--build-arg', 'UBUNTU_IMAGE=' + env['ubuntu_image'],
                '--build-arg', 'ENVIRONMENT_IMAGE=' + env_image,
                '--build-arg', 'SOURCE_SHA=' + cell['source_sha'],
                '--build-arg', 'WHEEL_VERSION=' + cell['version'],
                '--build-arg', 'RECIPE_SHA=' + cell['recipe'],
                '--build-arg', 'ENVIRONMENT_DIGEST=' + env_digest, '.')
        phase = 'qualification'
        labels = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image, '--format', '{{json .Config.Labels}}'], text=True))
        assert labels['io.pyasc.environment'] == env_digest, 'Environment digest differs'
        run('docker', 'run', '--rm', '--entrypoint', 'cat', image, '/opt/pyasc-image.json', stdout=open('work/installed-cell.json', 'w'))
        assert json.loads(Path('work/installed-cell.json').read_text()) == cell, 'Image identity differs'
        result['checks'] = [verify_image(image, mode, Path('work') / mode) for mode in ('identity', 'compile', 'model')]
        phase = 'publish'
        if not existing:
            run('docker', 'tag', image, cell['tag'])
            run('docker', 'push', cell['tag'])
            existing = inspect_image(cell['tag'])
        assert existing
        result['digest'] = existing
        result['image'] = cell['tag'].split(':')[0] + '@' + existing
        result['public_registry_check'] = verify_public_digest(result['image'])
        run('docker', 'pull', result['image'])
        result['public_identity_check'] = verify_image(result['image'], 'identity', Path('work/public'))
        result['status'] = 'pass'
    except Exception as error:
        result['phase'] = phase
        result['reason'] = str(error)
        log = Path('work/build.log')
        if log.exists():
            result['diagnostic_tail'] = log.read_text(errors='replace')[-5000:]
        raise
    finally:
        Path('work/result.json').write_text(json.dumps(result, indent=2) + '\n')


def finish():
    plan_data = json.loads(Path('work/plan.json').read_text())
    results = {r['id']: r for r in plan_data['previous']}
    for path in Path('results').glob('*/result.json'):
        result = json.loads(path.read_text())
        assert result['id'] in {c['id'] for c in plan_data['pending']}
        results[result['id']] = result
    cells = []
    for cell in plan_data['cells']:
        if 'incompatibility' in cell:
            cells.append({**cell, 'status': 'incompatible', 'reason': cell['incompatibility']['reason']})
            continue
        attempted = cell['id'] in {c['id'] for c in plan_data['pending']}
        cells.append(results.get(cell['id'], {**cell, 'status': 'failed' if attempted else 'not-run',
                                             'reason': 'No job result; inspect Actions logs' if attempted else 'Not selected in this run'}))
    manifests = []
    groups = {}
    for cell in cells:
        groups.setdefault(cell['key'].rsplit('-', 1)[0], []).append(cell)
    for pair in groups.values():
        if len(pair) != 2 or not all(c['status'] == 'pass' for c in pair):
            continue
        tag = merge_pair(*pair)
        try:
            digest = inspect_image(tag)
            if not digest:
                run('docker', 'buildx', 'imagetools', 'create', '-t', tag, *(c['image'] for c in pair))
                digest = inspect_image(tag)
            raw = json.loads(subprocess.check_output(['docker', 'buildx', 'imagetools', 'inspect', tag, '--raw'], text=True))
            actual = {m['platform']['architecture']: m['digest'] for m in raw['manifests']
                      if m.get('platform', {}).get('os') == 'linux'}
            assert actual == {c['environment']['arch']: c['digest'] for c in pair}, 'Mixed manifest children'
            manifests.append({'tag': tag, 'digest': digest, 'children': actual, 'status': 'pass'})
        except Exception as error:
            manifests.append({'tag': tag, 'status': 'failed', 'reason': str(error)})
    report = {'schema_version': 1, 'version': plan_data['publication']['version'],
              'source_sha': plan_data['publication']['source_sha'], 'recipe': plan_data['recipe'],
              'cells': cells, 'manifests': manifests,
              'scope': 'Upstream vector-add compile and simulator smoke, 128 float32 elements. No hardware or broad numerical qualification.'}
    Path('matrix.json').write_text(json.dumps(report, indent=2) + '\n')
    tag = plan_data['tag']
    existing = subprocess.run(['gh', 'release', 'view', tag, '--repo', REPO], capture_output=True)
    if existing.returncode:
        run('gh', 'release', 'create', tag, 'matrix.json', '--repo', REPO,
            '--target', os.environ['GITHUB_SHA'], '--title', 'Simulator matrix ' + report['version'],
            '--notes', 'Per-cell image digests and verification results are in matrix.json.')
    else:
        run('gh', 'release', 'upload', tag, 'matrix.json', '--clobber', '--repo', REPO)
    passed = sum(c['status'] == 'pass' for c in cells)
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write(f'Verified images: {passed}/{len(cells)}. Multiarch manifests: {len(manifests)}.\n')
    print(f'Verified {passed}/{len(cells)} images')
    if any(c['status'] == 'failed' for c in cells) or any(m['status'] == 'failed' for m in manifests):
        raise SystemExit('Some cells failed; the durable report lists their status and reason.')


if __name__ == '__main__':
    {'plan': plan, 'build': build_cell, 'finish': finish,
     'plan-environments': prepare_environments, 'environment': build_environment}[sys.argv[1]]()
