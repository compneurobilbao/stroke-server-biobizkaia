# compneuro-step-sdk

Python implementation of the step side of the [step contract 1.1](../../docs/contracts/step-contract.md) (manifests declaring 1.0 are also accepted and keep the 1.0 rules).
A step author writes one function, `run(ctx)`; the SDK does everything the contract requires around it.

```python
import sys
from stroke_step_sdk import main, InputRejected


def run(ctx):
    mask = ctx.input_path("lesion_mask")
    ...
    ctx.warn("small_lesion", "The lesion is very small.", port="lesion_mask")
    raise InputRejected(
        "mask_empty", "The lesion mask has no voxels set.", port="lesion_mask"
    )


sys.exit(main(run))  # /usr/local/bin/run
```

What the SDK does (section numbers refer to the contract):

| Contract | SDK |
|---|---|
| 1.1 manifest at `/opt/step/step.yaml` | loaded and validated with `contracts/validate.py` at start |
| 2.1 fixed command | `main()` parses `--in --out --params` (and `--help`) |
| 2.2 filesystem | staged inputs found as `/in/<port><ext>`; `ctx.output_path(port)`, `ctx.extra_path()`; stray files in `/out` are a failure |
| 1.4 parameters | defaults applied, validated against the manifest schema, `seed` helper |
| 2.3 environment | `STEP_CPUS`, thread variables, cache directories redirected to `/tmp` |
| 2.4 behaviour | input format checks against the artefact type registry; on `SIGTERM`, an `internal` error `terminated`, a `failed` provenance record and exit 143 |
| 3 logging | UTF-8 lines starting with a level word |
| 4 exit codes, `error.json` | `InputRejected` / `QcFailed` give exit 10, declared or reserved codes only (4.4: `input_missing`, `input_format_invalid`, `invalid_parameters` are raised by the SDK and implicit for 1.1 manifests; 1.0 manifests must declare them); other exceptions give exit 1 and an `internal` error; after exit 10 or 1 output files are removed |
| 5 provenance | `provenance.json` with hashes of every input and output (outputs only on success), bundles, software (`compneuro-step-sdk` and `python` added automatically), warnings; raw supplied parameters when they are rejected |

The JSON Schemas and the artefact type registry are not copied into the package: they are read from the
repository's `contracts/` directory. The SDK finds it through `STEP_CONTRACTS_DIR` (set in step images,
which copy `contracts/` to `/opt/step/contracts`) or by walking up from the package (development checkout).

`stroke-step-reftest` (module `stroke_step_sdk.reftest`) runs a step's `reference_test` under `local`,
`docker` or `apptainer` with the executor settings of contract section 6, checks the run directory the way
the orchestrator will, and runs `contracts/validate.py` on manifest, provenance and error file. If the
manifest names a `reference_test.generator` (contract 1.1, section 1.7), it runs that script first and fails
if a fixture is still missing.

Tests: `pip install -e steps/sdk[test] && pytest steps/sdk/tests`.
