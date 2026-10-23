"""Exercise the unchanged upstream add kernel; this is not a benchmark."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

mode = sys.argv[1]
cell = json.loads(Path('/opt/pyasc-image.json').read_text())
environment = cell['environment']
assert platform.machine() == {'amd64': 'x86_64', 'arm64': 'aarch64'}[environment['arch']]
assert platform.python_version() == environment['python_source']['version']
os_release = dict(line.rstrip().split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
assert os_release['VERSION_ID'].strip('"') == environment['ubuntu']
assert importlib.metadata.version('pyasc-dev') == cell['version']
assert not Path('/opt/llvm').exists() and not Path('/opt/pyasc').exists()
subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)
# Check the installed toolkit's own version record, not just the image label.
home = Path(os.environ['ASCEND_HOME_PATH']).resolve()
records = list(home.glob('**/ascend_toolkit_install.info')) + list(home.glob('version.cfg')) + list(home.glob('version.info'))
records += list(Path('/etc/Ascend').glob('*install.info')) if Path('/etc/Ascend').exists() else []
version_lines = [line for p in records for line in p.read_text(errors='replace').splitlines()
                 if line.lower().startswith(('version=', 'version =', 'version:'))]
assert any(line.replace(' ', '').split('=', 1)[-1].split(':')[-1] == environment['cann'] for line in version_lines), version_lines
os.environ['PYASC_CACHE_DIR'] = str(Path.cwd() / ('cache-' + mode))
os.environ['PYASC_DUMP_PATH'] = str(Path.cwd() / ('dump-' + mode))
os.environ['PYASC_DRY_RUN'] = '1' if mode == 'compile' else '0'
import asc
from asc.experimental import asctile
from asc.runtime import config
assert Path(asc.__file__).resolve().is_relative_to(Path('/opt/python'))
assert callable(asctile.jit)
result = {'status': 'pass', 'mode': mode, 'cell_id': cell['id'],
          'python': platform.python_version(), 'cann_version_records': version_lines}
if mode != 'identity':
    sys.path.insert(0, '/verification')
    from upstream_kernel import add
    import numpy as np
    config.set_platform('Model', environment['simulator'], check=True)
    x = np.arange(128, dtype=np.float32)
    y = np.full(128, 2, dtype=np.float32)
    out = np.full(128, np.nan, dtype=np.float32)
    add[1](x, y, out, 128, tile_length=128, unroll_factor=2,
           l2_cache=(False, False, True), always_compile=True)
    binary = Path(os.environ['PYASC_DUMP_PATH']) / 'binary.o'
    assert binary.stat().st_size > 0
    result['binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
    if mode == 'model':
        np.testing.assert_array_equal(out, np.arange(128, dtype=np.float32) + np.float32(2))
        np.save('actual.npy', out)
    else:
        assert np.isnan(out).all()
Path('result-' + mode + '.json').write_text(json.dumps(result, indent=2) + '\n')
