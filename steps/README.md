# Steps

A step is one container image plus one manifest, `step.yaml`, that follows the [step contract 1.0](../docs/contracts/step-contract.md).
This directory holds the SDK every step is built on and the template to copy.

| Path | Content |
|---|---|
| `sdk/` | `compneuro-step-sdk` (Python package `stroke_step_sdk`): the step side of the contract, with unit tests in `sdk/tests/` |
| `template/` | A trivial working step (lesion volume on an MNI mask): manifest, code, `Dockerfile`, `Apptainer.def`, tests |
| `<step name>/` | One directory per step of this repository (added by later cards) |

## Create a new step from the template

1. Copy the directory: `cp -r steps/template steps/<new-step>` (kebab-case name).
2. Edit `step.yaml`: `name`, `version`, `title`, `provides` (the pipeline slot), `image.repository`, `licence`, `inputs`, `outputs` (artefact types from [`contracts/artefact-types.yaml`](../contracts/artefact-types.yaml)), `parameters` (every parameter needs a default; declare `seed` if the step is stochastic), `resources`, `errors` and `reference_test`. Keep the three codes `input_missing`, `input_format_invalid` and `invalid_parameters` in `errors`: the SDK raises them and refuses to emit a code that is not declared.
3. Write the step in `src/<package>/step.py` as one function, `run(ctx)`. Read inputs with `ctx.input_path(port)`, parameters from `ctx.params`, write each output to `ctx.output_path(port)`. Raise `InputRejected(code, message, port=...)` or `QcFailed(...)` for exit 10 with a declared code; call `ctx.warn(code, message)` for non-fatal findings; anything else that escapes is an internal failure (exit 1). Rename the package in `bin/run` and the `Dockerfile` if you change it.
4. Write the reference dataset generator (`tests/make_reference.py`) and the tests. The dataset must be synthetic or public with a redistributable licence and is referenced from `reference_test` in the manifest; the template generates it at test time and does not commit it.
5. Adjust the `Dockerfile` (base image, tool installation with pinned versions; keep the mount points, permissions and the SDK/contracts copy) and `Apptainer.def`.
6. Add the step to CI by copying the `template-container` job of [`.github/workflows/steps.yml`](../.github/workflows/steps.yml) with your paths and image name.

## Check a step locally

```sh
pip install -r steps/sdk/requirements-image.txt pytest && pip install --no-deps -e steps/sdk
python contracts/validate.py --kind step steps/<new-step>/step.yaml
python -m pytest steps
python steps/<new-step>/tests/make_reference.py
docker build -f steps/<new-step>/Dockerfile -t my-step:dev .
stroke-step-reftest --step-dir steps/<new-step> --mode docker --image my-step:dev
apptainer build --build-arg IMAGE=my-step:dev step.sif steps/<new-step>/Apptainer.def
stroke-step-reftest --step-dir steps/<new-step> --mode apptainer --sif step.sif
```

`stroke-step-reftest` stages the reference inputs, runs the step with the executor settings of contract section 6 (read-only root, arbitrary UID, no capabilities, no network under Docker; `--containall --cleanenv --no-home` under Apptainer), then checks the result like the orchestrator will: exit code, only allowed files in `/out`, hashes in `provenance.json`, output formats, `contracts/validate.py` on manifest and provenance, and the `expected` comparisons of the manifest. `--mode local` runs `bin/run` from the working tree without a container.

## Rules the SDK enforces for you

- the fixed command `/usr/local/bin/run --in /in --out /out --params /in/params.json`; the manifest is read from `/opt/step/step.yaml`;
- inputs are checked against their artefact type (gzip NIfTI, binary values, voxel grid, JSON schema, TSV columns, PDF/PNG signature) before `run` starts; outputs are checked before exit 0;
- parameters: defaults applied, validated against the manifest schema;
- `provenance.json` on exit 0, 10 and on internal failure; `error.json` on failure; nothing else in `/out` except `extra/`;
- thread variables follow `STEP_CPUS`; caches go to `/tmp`; `SIGTERM` stops the step and writes a failure report within the executor's grace period.

What stays your job: no network, no writes outside `/out` and `/tmp`, determinism (derive randomness from `ctx.rng()`), no identifiers or file contents in logs and messages, and a Dockerfile that satisfies contract section 6.

## Image notes

- The build context is the repository root, because the image bundles `steps/sdk` and the `contracts/` schemas (found by the SDK through `STEP_CONTRACTS_DIR`).
- The SIF for production is built from the registry image by digest (`apptainer build step.sif docker://<repo>@sha256:...`); `Apptainer.def` documents the local variant used in CI.
- The CI image is not pushed anywhere; publishing and the registry belong to later cards.
