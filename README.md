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
No host PyAsc checkout is needed. The entrypoint preserves an explicitly supplied
`PYTHONPATH` but excludes CANN's optional profiler Python distributions from the
managed Python environment. Compiler and simulator paths remain initialized.

To change only PyAsc, keep the CANN, Ubuntu, Python and architecture fields
fixed and select another wheel publication. To compare CANN versions, keep
the source commit, wheel hash, Ubuntu, Python and architecture fixed.
Record the chosen image digest with every result. Descriptive tags include
the wheel version, CANN, Ubuntu, Python, recipe identity and architecture;
they are never overwritten. There is no ambiguous `latest` experiment tag.

To vary only CANN, use one release's `matrix.json` and keep Ubuntu, Python,
architecture, source and wheel fixed:

```bash
for cann in 9.0.0 9.2.0; do
  image=$(jq -er --arg cann "$cann" '
    .cells[] | select(.status == "pass"
      and .environment.ubuntu == "22.04"
      and .environment.python == "3.11"
      and .environment.arch == "amd64"
      and .environment.cann == $cann) | .image' matrix.json) || exit 1
  docker run --rm --ulimit nofile=65536:65536 \
    -v "$PWD:/workspace" "$image" python3 /workspace/experiment.py
done
```

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

To prepare reusable environments before a wheel publication, dispatch
`environments.yml` with explicit cell keys. This builds no compiler or simulator
wheel; final images still undergo the full qualification pipeline. CANN's
version-specific installer arguments are recorded alongside each installer hash.

Complete wheel manifests can include explicit upstream incompatibilities. These
remain visible in all affected matrix cells and are not retried as image-build
failures. In particular, the frozen source uses `dataclass(slots=True)` and cannot
import on Python 3.9. The native wheel checks retain evidence for both architectures;
no source compatibility patch or broken Python 3.9 wheel is substituted.

## Development overlays

Development images are published separately as `ghcr.io/compiler-team-ru/pyasc-sim-dev`.
They inherit an exact verified simulator image digest and add CPU Torch 2.7.1+cpu,
pytest 8.4.2, and checksum-pinned dependencies. They do not rebuild the compiler,
replace base dependencies, or include upstream source. Minimal images stay minimal.

The `dev-images.yaml` workflow discovers completed base matrices hourly. Manual
inputs select an exact base matrix release tag (`report`) and optional cell keys
(`cells`). Publication requires dependency consistency, preserved base packages,
native imports, CPU Torch operations, collection of every AscTile test, and the
baseline compile/simulator check. Published digests are pulled and checked again.
Each `dev-matrix.json` records the base and overlay digests and environment evidence.
`published` means **environment-qualified**, not that the full suite passed.

Full execution continues independently in `dev-qualification.yaml`, triggered after
publication and hourly, with the same manual report/cell selection. Its durable
`qualification-<recipe>.json` assets record the current full-suite status, counts,
individual failures and evidence links. The environment report's `suite` field
records the state **at publication**; consult the separate qualification report for
subsequent results. Missing or interrupted module checkpoints are retried; completed
ones are reused for identical image and qualification inputs. Python 3.9 retains
the base publication's incompatibility.

Use the source commit recorded in the report and mount its checkout:

```bash
docker run --rm --ulimit nofile=65536:65536 \
  -v "$PWD:/source:ro" -w /tmp \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYASC_CACHE_DIR=/tmp/pyasc-cache -e PYASC_DUMP_PATH=/tmp/pyasc-dumps \
  ghcr.io/compiler-team-ru/pyasc-sim-dev@sha256:<overlay-digest> \
  python3 -m pytest -c /source/pyproject.toml \
    /source/experimental/asctile/python/test --backend Model --platform Ascend950PR_9599
```

For one test, replace the suite path with its module or exact pytest node ID.
To vary only CANN, select two published cells in the same dev report with identical
Ubuntu, Python, architecture, source, wheel hash and overlay recipe, differing only
in `base.environment.cann`. Use their recorded overlay digests.

Qualification collects every module and executes all parameterizations with upstream
assertions and markers. Every module runs in a separate container with fresh caches;
its logs, JUnit XML and per-test outcomes are retained as release assets. Completed
module checkpoints are reused only for the same overlay identity. Dependency, collection, environment and baseline failures block publication.
Full-suite failures or interruptions remain explicitly non-green in the separate
qualification report and do not overwrite or withdraw environment-qualified images.
Exact reviewed diagnoses distinguish compiler, numerical and platform failures;
unreviewed failures remain unclassified. No new skips, expected failures or reduced parameter sets
are introduced. Upstream skips, xfails and unexpected passes remain visible.

### Discovery and stable reports

The intended policy is an overlay for every complete base publication, not only
explicit requests. `dev-images.yaml` checks at minute 47 each hour (UTC), selecting
the oldest unfinished complete base report and processing one publication per run.
A failed older publication can delay newer ones; an exact `report` input bypasses
that queue. A schedule is a requested cadence, not a completion deadline. The
separate qualification workflow checks at minute 57 and after successful overlay
runs; its concurrency group does not block overlay publication.

Development reports and test checkpoints are GitHub prereleases. Both development
workflows run `dev/releases.py` to repair historical release metadata and keep the
newest complete base report at:

<https://github.com/compiler-team-ru/pyasc-images/releases/latest/download/matrix.json>

Use exact release URLs and image digests for reproducible experiments. Release
metadata repair does not change a base report, image, wheel, or recipe.

Python 3.9 exclusions are source-specific. The October 23 base records an upstream
import incompatibility; the October 24 base passed all 32 minimal cells. The current
development dependency lock covers only Python 3.10–3.12. Its eight Python 3.9
candidates therefore remain excluded from the overlay, with a separate lock-scope
reason; this is not evidence that the newer base or upstream source is broken.

### Qualification report contract

For an exact development release `<dev-release>`, the stable qualification URL is:

```
https://github.com/compiler-team-ru/pyasc-images/releases/download/<dev-release>/qualification.json
```

It is created when qualification is planned and updated when the matrix run finishes.
Before planning it may be absent; a missing report means **no full-suite conclusion**.
The alias has identical content to `qualification-<qualification_recipe[:16]>.json`.
The full `qualification_recipe` hashes `dev/check.py`, `dev/evidence.py`,
`dev/pipeline.py`, and `dev/model.py`; it is neither the overlay image recipe nor the
base recipe. Changes to those qualification inputs invalidate checkpoint reuse.
Recipe-specific reports remain available when the alias advances. Both URLs are
mutable progress snapshots, not immutable test certificates.

Schema version 1 contains `base_report`, `base_report_sha256`, `recipe` (overlay),
`qualification_recipe`, and `cells`. Cell identity includes `key`, `id`, `base`
(including `source_sha`), and the published overlay `image`/`digest` when available.
The environment report's `suite` and manifest `suite_status` are publication-time
snapshots; they do not advance as qualification progresses.

| Qualification cell | Meaning for a consumer |
|---|---|
| `status: pending` | `not-run` / no completed evidence for this recipe; execution may be in progress |
| `status: complete`, `suite.suite_status: passed` | Complete execution with no unexpected failures; inspect skip/xfail counts |
| `status: complete`, `suite.suite_status: failures` | `failed`; exact failures and reviewed/unreviewed diagnoses are recorded |
| `status: incomplete` or `blocked` | `blocked`; inspect `phase`, `reason`, `run_url`, and any `partial_suite` |
| `status: incompatible` | Excluded with an explicit base or overlay reason; never a suite pass |

A complete `suite` has `inventory` (exact node IDs), `collected`, `executed`,
`suite_status`, and `modules`. Each module has `module`, `counts` (`passed`, `failed`,
`skipped`, `xfail`, `xpass` as present), `failures`, `collected`, `executed`,
`suite_status`, `evidence_url`, and `evidence_sha256`. `publishable` is the historical
name for the diagnosis review gate; it does not control environment publication.

Each module archive contains `identity.json`, `evidence.json`, `junit.xml`, and
`output.log`. `evidence.json` contains the collected `inventory`, pytest `exitstatus`,
and per-case phase events: `nodeid`, `phase` (setup/call/teardown), `outcome`, `xfail`,
`detail`, and `duration`. JUnit also records per-case outcomes. Upstream skipped and
expected-failure cases must retain those outcomes, rather than being counted as
numerical passes. Strict unexpected passes remain failures.

**Interruption limitation:** the current plugin writes evidence at session end.
OOM, timeout, or a killed runner may leave no evidence for the interrupted module.
A cell is then incomplete/blocked, not a failed kernel. `partial_suite` contains
only finished modules; an absent module may have been interrupted or never started.
The report does not yet supply a reliable four-state per-case inventory for that
situation, nor does exit 137 prove OOM. Consumers must keep unresolved modules
`blocked`/unknown until logs and host evidence diagnose them; they must not infer
`failed` or `not-run` from absence alone. Runner limits are two CPUs and 12 GiB per
container, up to three hours per module and six hours per matrix job. These are
resource limits, not kernel correctness criteria.

### Consuming upstream tests

Use the test tree at the **same source commit as the installed wheel**. Keep the
parent `conftest.py` and configuration when selecting a subtree. Based on the
fixtures at `d0f64a63c432761aa57648e67be7582780a17561`:

| Subtree | What its checks exercise |
|---|---|
| `unit/` | Frontend/IR/API checks; the autouse fixture mocks the compiler and launcher and verifies generated IR. This is not simulator numerical execution. |
| `operations/` | Operation-level JIT execution with reference checks, including Torch references. Useful as a separate simulator correctness gate. |
| `target/` | Larger target kernels, including `matmul_v3/` and `kimi_k3/`; the subtree requires C310 and includes resource-heavy cases. |
| `kernels/` | Additional kernels, also requiring C310; not covered by selecting `target/` alone. |

`target/` is a reasonable focused downstream gate, not the entire upstream
correctness reference. `unit/` and `operations/` are also usable checks, with their
fixture semantics kept distinct. The shared default is backend `Model`, platform
`Ascend950PR_9599`, and CPU Torch. NPU/profiling paths have different requirements.
These fixtures establish that simulator execution is supported, not that every
parameterization passes on every matrix cell or fits a hosted runner.

Do not describe `--compile-only` as numerical qualification: upstream mocks launch
and numerical assertions in that mode. Keep upstream skip/xfail reasons and separate
resource blockers from compiler or numerical failures. The old 4,293-case inventory
belongs to `ea2dbf9d`; use each newer report's collected inventory, not a fixed count.
