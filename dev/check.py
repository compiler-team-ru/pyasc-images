"""Check development dependencies without changing the base environment."""
import importlib.metadata as metadata
import json
from pathlib import Path
import subprocess

subprocess.run(['python3', '-m', 'pip', 'check'], check=True)
for lock in ('/opt/requirements.lock', '/opt/dev-requirements.lock'):
    for line in Path(lock).read_text().splitlines():
        if '==' in line and not line.startswith((' ', '#')):
            name, version = line.split()[0].split('==')
            assert metadata.version(name) == version, (name, metadata.version(name), version)
import torch
import pytest
import asc
from asc._C import libpyasc
assert torch.__version__ == '2.7.1+cpu'
assert torch.version.cuda is None
assert pytest.__version__ == '8.4.2'
assert not any(d.metadata['Name'].lower().startswith(('nvidia-', 'torch-npu')) for d in metadata.distributions())
x = torch.arange(32, dtype=torch.float32)
torch.testing.assert_close(x + x, torch.arange(32, dtype=torch.float32) * 2)
assert x.device.type == 'cpu'
assert str(Path(asc.__file__).resolve()).startswith('/opt/python/')
print(json.dumps({'status': 'pass', 'torch': torch.__version__, 'pytest': pytest.__version__,
                  'dependencies': {d.metadata['Name']: d.version for d in metadata.distributions()}}))
