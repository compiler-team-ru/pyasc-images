# PyAsc simulator images

Build and verify a simulator environment independently of compiler builds.
Images install exact published `pyasc-dev` wheels; they never compile PyAsc
or LLVM. Wheel publication is maintained in
[asctile](https://github.com/compiler-team-ru/asctile).

## Matrix

| Dimension | Values |
| --- | --- |
| Python | 3.9, 3.10, 3.11, 3.12 |
| CANN | 9.0.0, 9.2.0 |
| Ubuntu | 22.04, 24.04 |
| Architecture | amd64, arm64 |

These are 32 candidate combinations, not an assertion that every combination
is supported by CANN. Each matrix release contains `matrix.json` with the
actual status, image digest, input hashes, and verification evidence per cell.
Only verified cells receive simulator image tags. Multiarch manifests require
both matching architecture cells to pass.

The initial source is pinned in `inputs.lock.json`. The hourly workflow
discovers complete wheel manifests beginning with that source's publication.
It processes pending releases in order. Manual dispatch can select a wheel
version and comma-separated cell keys, for example
`u22.04-c9.0.0-p3.11-amd64`. A repeated run skips verified cells and retries
failed or missing cells.

## Run an experiment

Select an image from a matrix release and use its digest:

```bash
docker run --rm -it \
  --ulimit nofile=65536:65536 \
  -v "$PWD:/workspace" -w /workspace \
  ghcr.io/compiler-team-ru/pyasc-sim@sha256:<verified-digest> bash
```

The entrypoint initializes CANN and the configured simulator. Python is under
`/opt/python`, with matching headers and a C++ compiler available for JIT.
No host PyAsc checkout is needed.

To change only PyAsc, keep the CANN, Ubuntu, Python and architecture fields
fixed and select another wheel publication. To compare CANN versions, keep
the source commit, wheel hash, Ubuntu, Python and architecture fixed.
Record the chosen image digest with every result. Descriptive tags include
the wheel version, CANN, Ubuntu, Python, recipe identity and architecture;
they are never overwritten. There is no ambiguous `latest` experiment tag.

## Build inputs and contents

`inputs.lock.json` pins Ubuntu image digests, CPython source archives,
CANN toolkit installers and the simulator target. `requirements.lock`
pins Python dependency versions and hashes. Update these through review.
No source or environment version is silently substituted when a cell fails.

The shared environment layer contains Ubuntu, Python, CANN, the simulator,
and runtime/JIT dependencies. Its content identity excludes the PyAsc wheel.
Verified environment images and registry build caches are reusable across
compiler releases. The final layer installs only the checksum-verified wheel
and records its complete input identity in `/opt/pyasc-image.json`.

Final images exclude source checkouts, LLVM build trees, agents, experiment
code, and unconditional Torch installation. CPython and CANN license files
remain present; PyAsc and LLVM notices remain in the wheel.

## Verification

Every image must pass OS, architecture, Python, toolkit-version, dependency,
and native-import checks. CI then extracts the unchanged upstream `add`
kernel from the exact PyAsc source commit, recording the original and
extracted hashes. This avoids importing the upstream Torch test harness.
Compilation and simulator execution use separate fresh caches. The 128-element
float32 result is checked against an independent NumPy expression.

After publication, CI pulls the image by digest and repeats identity/import
checks. Manifest assembly verifies both architecture digests. This is a narrow
simulator smoke test, not broad numerical qualification or hardware evidence.

Existing skill-stack experiment pins and consumer workflows are not updated
by this repository.
