"""Pure matrix expansion and identity checks shared by CI and tests."""
import hashlib
import itertools
import json
from pathlib import Path

ARCH = {'amd64': 'x86_64', 'arm64': 'aarch64'}
REGISTRY = 'ghcr.io/compiler-team-ru'


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def recipe_hash(root):
    paths = [root / 'Dockerfile', root / 'entrypoint.sh', root / 'requirements.lock', root / 'inputs.lock.json',
             *sorted((root / 'ci').glob('*.py')), *sorted((root / '.github/workflows').glob('*.yml'))]
    return identity({str(p.relative_to(root)): file_hash(p) for p in paths if not p.name.startswith('test_')})


def validate_publication(publication):
    assert publication['schema_version'] == 1 and publication['status'] == 'complete'
    assert publication['source_repository'] == 'compiler-team-ru/pyasc'
    expected = {f'cp{p.replace(".", "")}-{a}' for p in ('3.9', '3.10', '3.11', '3.12') for a in ARCH.values()}
    assert set(publication['wheels']) == set(publication['targets']) == expected
    for target, wheel in publication['wheels'].items():
        assert wheel['sha256'] == publication['targets'][target]
        assert Path(wheel['filename']).name == wheel['filename']
        assert wheel['filename'].startswith('pyasc_dev-' + publication['version'] + '-')
        python_tag, arch = target.split('-', 1)
        assert f'-{python_tag}-{python_tag}-' in wheel['filename']
        assert wheel['filename'].endswith('_' + arch + '.whl')
        assert wheel['url'].startswith('https://files.pythonhosted.org/')
        assert wheel['llvm']['revision']


def environments(lock, recipe):
    assert all(lock[k] for k in ('ubuntu', 'cann', 'python')), 'Incomplete input lock'
    cells = []
    for ubuntu, cann, python, arch in itertools.product(lock['ubuntu'], lock['cann'], lock['python'], ARCH):
        environment = {'ubuntu': ubuntu, 'ubuntu_image': lock['ubuntu'][ubuntu][arch],
                       'python': python, 'python_source': lock['python'][python],
                       'cann': cann, 'cann_installer': lock['cann'][cann][arch],
                       'arch': arch, 'simulator': lock['simulator'], 'recipe': recipe}
        env_id = identity(environment)
        cells.append({'key': f'u{ubuntu}-c{cann}-p{python}-{arch}', 'environment': environment,
                      'environment_id': env_id, 'environment_image': f'{REGISTRY}/pyasc-env:{env_id}',
                      'runner': 'ubuntu-24.04-arm' if arch == 'arm64' else 'ubuntu-24.04'})
    return cells


def expand(lock, publication, recipe, environment_recipe):
    validate_publication(publication)
    assert all(lock[k] for k in ('ubuntu', 'cann', 'python')), 'Incomplete input lock'
    cells = []
    for base in environments(lock, environment_recipe):
        environment = base['environment']
        ubuntu, cann, python, arch = (environment[k] for k in ('ubuntu', 'cann', 'python', 'arch'))
        target = f'cp{python.replace(".", "")}-{ARCH[arch]}'
        env_id = base['environment_id']
        cell = {'key': f'u{ubuntu}-c{cann}-p{python}-{arch}', 'environment': environment,
                'environment_id': env_id, 'environment_image': f'{REGISTRY}/pyasc-env:{env_id}',
                'version': publication['version'], 'source_sha': publication['source_sha'],
                'wheel': publication['wheels'][target], 'recipe': recipe,
                'runner': 'ubuntu-24.04-arm' if arch == 'arm64' else 'ubuntu-24.04'}
        cell['id'] = identity(cell)
        cell['tag'] = f'{REGISTRY}/pyasc-sim:v{cell["version"]}-cann{cann}-u{ubuntu}-py{python}-s{cell["source_sha"][:12]}-r{recipe[:16]}-{arch}'
        cells.append(cell)
    return cells


def pending(cells, previous, selected=''):
    keys = set(filter(None, selected.split(',')))
    assert not keys or keys <= {c['key'] for c in cells}, 'Unknown matrix cell'
    passed = {r['id'] for r in previous if r['status'] == 'pass'}
    return [c for c in cells if c['id'] not in passed and (not keys or c['key'] in keys)]


def merge_pair(left, right):
    assert {left['environment']['arch'], right['environment']['arch']} == {'amd64', 'arm64'}
    for key in ('version', 'source_sha', 'recipe'):
        assert left[key] == right[key], 'Mixed publication or recipe'
    for key in ('ubuntu', 'cann', 'python', 'simulator'):
        assert left['environment'][key] == right['environment'][key], 'Mixed environment'
    assert left['status'] == right['status'] == 'pass'
    return left['tag'].rsplit('-', 1)[0]
