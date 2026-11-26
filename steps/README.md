# Steps

A step is one container image plus one manifest, `step.yaml`, that follows the [step contract 1.0](../docs/contracts/step-contract.md).
This directory holds the SDK every step is built on and the template to copy.

| Path | Content |
|---|---|
| `sdk/` | `compneuro-step-sdk` (Python package `stroke_step_sdk`): the step side of the contract, with unit tests in `sdk/tests/` |
| `template/` | A trivial working step (lesion volume on an MNI mask): manifest, code, `Dockerfile`, `Apptainer.def`, tests |
| `<step name>/` | One directory per step of this repository, including the wrappers of the lab's tools in the authors' repositories (`lnm-tracts`, `ct-lesion-to-mni152`, `cognet-stroke`, and `slnm-coarse`, in development) |
| `fetch-upstream.sh` | Fetches the author's repository of a wrapper at the commit of its `upstream.env` (CI, local tests); a private repository is read with `UPSTREAM_TOKEN` (or `GITHUB_TOKEN`), exit code 3 when it is not readable |
| `run-step.sh` | Runs a step image once with the contract section 6 flags (CI helper) |

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
- The CI images are built into throwaway registries and never published. Releases go through `.github/workflows/release-steps.yml` (`pipelines/stroke-lnm/release.py`): each step of `registry.yaml` is built from its pinned source, its reference test runs against the image, and only then is it pushed to `docker.io/compneurobilbaolab/step-<name>:<step version>` (immutable tags, never `build: local` entries); `registry.py pin` writes the published digests into the registry and the pipeline. A new step enters the release through its registry entry and a line in `PREPARE` of `release.py` (how its reference case is generated), unless its manifest declares `reference_test.generator`. See `docs/deployment.md`, "Publishing step images".
- Licence note for published images: the platform is non-commercial research software. The `ct-lesion-to-mni152` image contains FSL, whose licence is non-commercial for the general public; the project owner accepts publishing it on that basis (`licence_review` of its registry entry). Details in [`ct-lesion-to-mni152/README.md`](ct-lesion-to-mni152/README.md).

## Wrapping a tool of an author's repository

The lab's scientific tools live in their authors' repositories ([lnm](https://github.com/compneurobilbao/lnm), [CTLesion2MNI152](https://github.com/compneurobilbao/CTLesion2MNI152), [COGNET-STROKE](https://github.com/compneurobilbao/COGNET-STROKE)). **Their wrappers live here, never there**: the authors' repositories are left untouched (no `step/` folder, no CI workflow, no pull requests from the platform), and a wrapper consumes the author's code unmodified at a pinned commit, the way [`flnm-fast`](flnm-fast/) consumes a third-party image by digest. The pattern (card STEPS-01; examples [`cognet-stroke`](cognet-stroke/), [`lnm-tracts`](lnm-tracts/), [`ct-lesion-to-mni152`](ct-lesion-to-mni152/)):

1. `steps/<name>/upstream.env` names the repository and the commit (`UPSTREAM_REPOSITORY=<owner>/<name>`, `UPSTREAM_COMMIT=<40 hex>`), the files the wrapper takes from it (`UPSTREAM_PATHS`) and the SHA-256 of their content (`UPSTREAM_SHA256`, computed by `steps/upstream-digest.sh`; the Dockerfile and `steps/fetch-upstream.sh` fail on a mismatch). The registry entry's source is `{repository: this, dockerfile: steps/<name>/Dockerfile, upstream: {repository, commit}}` with the same values; `registry.py check` requires them to agree, and requires `source.upstream` for every step directory that has an `upstream.env`.
2. The `Dockerfile` downloads the archive of that commit (`codeload.github.com/<repository>/tar.gz/<commit>`) in a build stage, takes only what the step needs (a module, the package, a script for a CI comparison) and copies or installs it unchanged; the rest of the image is built like any other step of this repository (SDK and contracts from here). The image records the commit (`/opt/upstream/<name>/UPSTREAM_COMMIT`). A step that ports the author's algorithm instead (`lnm-tracts`) ships no upstream code; its `upstream` then only names the version CI compares against.
3. The wrapper adapts and never re-implements: it calls the author's functions and handles around them what the contract needs (input checks, undefined values, degenerate cases, provenance). If the author's code misbehaves on a case the wrapper must handle, the wrapper works around it and documents it; the fix is the author's.
4. Tests and CI fetch the same commit with `steps/fetch-upstream.sh steps/<name> DEST`, check that the image carries the author's code byte for byte, and compare the wrapper's results with direct calls of the author's code. A wrapper that replaces an image built from the author's repository is compared with that published image on the same inputs.
5. Moving to another commit of the author's repository is a new step version (`upstream.env` and the registry entry change together), with the equivalence evidence in the pull request.

## Wrapping an external tool

A third-party tool enters the platform as a *wrapper step* that fills a slot next to the lab's own implementations ([run-plans.md](../docs/design/run-plans.md#4-external-tools-per-slot), [ADR-0018](../docs/adr/0018-presets-free-mode-external-tools.md)). The wrapper is an adapter: it converts formats and names, and it never changes the tool's algorithm. The worked example is [`flnm-fast`](flnm-fast/) (registry entries `functional-disconnection/flnm-fast-hcp-aging` and siblings, step version 3.0.0, contract 1.2). Work through this checklist and put the answers in the pull request; `python pipelines/<id>/registry.py check` enforces what a machine can check.

**Checklist**

1. **Licence and data-use review.** Record it in the registry entry as `licence_review: {status, code, weights, image_redistribution, notes, reviewed, reviewed_by}`.
   - `code`: SPDX identifier of the upstream code; it must equal `upstream.licence` in the manifest. The licence must permit research use.
   - `weights`: licence of the model weights or reference data, and whether they may be redistributed. This is separate from the training-data terms: record those too, in `notes` or the bundle's model card. Weights trained under a data-use agreement that forbids distributing derived models are not accepted. A tool whose licence forbids research use, or requires a registration per user, is not accepted.
   - `image_redistribution`: `allowed` if an image containing the tool may be pushed to the public registry, otherwise `forbidden` (see item 10).
   - `notes`: restrictions, citation duties, and every known defect of the tool that the wrapper works around.
   - `status`: `reviewed` needs `reviewed` (`YYYY-MM-DD`) and `reviewed_by`; `pending` (not yet confirmed) has neither. `reviewed_by` is the responsible scientist or the licence owner's confirmation, **not the wrapper author**. Do not invent a date to look reviewed.
   - Never commit paths, URLs or credentials of restricted artefacts when their terms forbid it: the repository names the artefact (name, version, checksum) and the deployment supplies it.
   - Review again whenever the tool's code, its weights or any of their licences change; a new upstream release is a new step version and a new review.
2. **Origin and manifest.** Set `origin: external` on the registry entry. The step manifest declares `contract_version: "1.2"` and an `upstream` block (`name`, `version`, `homepage`, `image` by digest, `licence`, and a `citation`, which is required for external entries because the report cites it); the tool is also listed in `licence.third_party[]` with the same name and licence ([step-contract.md 1.8](../docs/contracts/step-contract.md#18-external-tools-the-upstream-block)). A manifest change is a new step version, pinned by a new (minor) pipeline version.
3. **Digest pin.** The `Dockerfile` starts `FROM <upstream image>@sha256:<64 hex>` (every base image, no tags, no variables in `FROM`), and `upstream.image` is exactly that digest. Anything else the build downloads (packages, checkpoints) is pinned by version or checksum.
4. **Adapter only.** Do not edit the tool. A processing choice that changes results (threshold, resampling) is a manifest parameter with a default, recorded in provenance. If the tool has a defect, work around it in the wrapper (warn, refuse the affected input with a declared error code) and record it in `licence_review.notes` and the validation notes; the fix belongs to the tool's author.
5. **No network, enforced.** Steps run without network access: configure the tool to read everything from `/in` and `/ref`, and run the reference test with networking disabled. The `docker` executor enforces it (`--network none`); the `subprocess` executor does not (development only), and the `slurm` executor is not implemented yet, so an external tool must not be enabled where isolation is not enforced.
6. **Resources declared.** The manifest `resources` states CPUs, memory, scratch, wall time and, if needed, a GPU, with values measured on the reference case; a tool that needs a GPU or HPC-class memory says so in the registry `notes`, because the executor and the deployment must provide it.
7. **Weights and data as bundles.** Model weights and reference data are reference bundles (`bundle.yaml`, [bundles.md](../docs/contracts/bundles.md)) mounted read-only under `/ref`: licence, `redistributable`, citation and version are recorded there. Nothing of the kind goes into Git or into the image unless its licence allows redistribution, and non-redistributable weights never reach public CI.
8. **Reference cases.** The manifest has a `reference_test` on synthetic or public data with a licence that allows redistribution (never patient data); the expected result is computed independently of the tool where possible. The test runs in CI against the built image. A model whose weights cannot be redistributed reaches `reference-tested` in one of two ways: the reference test runs in CI with tiny redistributable test weights (a stand-in bundle that exercises the wrapper, not the model), or the responsible scientist runs the real weights locally on a public or synthetic case and commits a **result summary** (metrics and checksums, no weights, no paths, no data) next to the registry entry, referenced in `validation.notes`. In both cases the real weights stay out of public CI.
9. **Validation level.** Set `validation: {status, record, reviewed_by, notes}` in the registry entry; every real implementation needs an explicit status, and nothing is validated implicitly: `unvalidated` (manifest valid; CI and demo deployments only), `reference-tested` (item 8 satisfied for this digest; allowed on `on-premise`) or `validated` (agreement with reference results on reference cases, run by lab staff and **signed off by the responsible scientist**, who is `reviewed_by`, not the wrapper author; `record` is a document in the repository without data; the licence review must be `reviewed`). Agents never claim `validated`. A deployment enables, and its plans and runs use, only implementations at or above its `MIN_IMPLEMENTATION_VALIDATION` (`validated` on `research-public` and `mask-only`, `reference-tested` by default on `on-premise`); the admin endpoint and `seed_implementations` refuse the rest with `implementation_not_validated`.
10. **Local build when the licence forbids publishing.** If the licence forbids publishing an image that contains the tool, set `licence_review.image_redistribution: forbidden` and `build: local` (the check requires both together, and refuses a public-registry `image` for the entry). The wrapper's source stays public; CI neither builds nor pushes it (`registry.py build` skips it); each deployment builds the image itself from the pinned source and the upstream artefact it obtained under the licence, pins the digest of its own build in the materialised pipeline (`docs/deployment.md`, "Building a `build: local` wrapper"), and installers do not include it.
11. **Labelling.** The orchestrator copies the `upstream` block into the run's slot record, and the interface, provenance and report label the result as produced by an external tool, with its name, version and licence. Labelling by validation status: the slot listing, the admin list, the wizard and the run page show the level and an "experimental" badge for `reference-tested`; the run page shows the level in effect when the run was created. A validation status in the slot record and in the report needs contract 1.3 (not part of this card).
