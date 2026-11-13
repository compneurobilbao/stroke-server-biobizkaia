# compneuro-step-sdk

Python implementation of the step side of the [step contract 1.0](../../docs/contracts/step-contract.md).
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
| 2.4 behaviour | input format checks against the artefact type registry; `SIGTERM` handling |
| 3 logging | UTF-8 lines starting with a level word |
| 4 exit codes, `error.json` | `InputRejected` / `QcFailed` give exit 10, declared codes only; other exceptions give exit 1 and an `internal` error |
| 5 provenance | `provenance.json` with hashes of every input and output, bundles, software, warnings |

The JSON Schemas and the artefact type registry are not copied into the package: they are read from the
repository's `contracts/` directory. The SDK finds it through `STEP_CONTRACTS_DIR` (set in step images,
which copy `contracts/` to `/opt/step/contracts`) or by walking up from the package (development checkout).

`stroke-step-reftest` (module `stroke_step_sdk.reftest`) runs a step's `reference_test` under `local`,
`docker` or `apptainer` with the executor settings of contract section 6, checks the run directory the way
the orchestrator will, and runs `contracts/validate.py` on manifest, provenance and error file.

Tests: `pip install -e steps/sdk[test] && pytest steps/sdk/tests`.
