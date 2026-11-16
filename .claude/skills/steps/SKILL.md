---
name: steps
description: Guide for processing steps under the step contract (step.yaml, SDK, template, provenance, exit codes, Docker and Apptainer, contracts/validate.py). Use when creating or wrapping a processing tool, or changing steps/, contracts/ or fixtures/.
---

# Steps and the step contract

- Normative spec: `docs/contracts/` (start with `step-contract.md`; also `pipeline.md`, `provenance.md`, `bundles.md`, `artefact-types.md`, `versioning.md`). Schemas and examples: `contracts/`. ADRs 0001-0008 and 0015.
- A step is a container image plus `step.yaml`; command `run --in /in --out /out --params /in/params.json`; reads only `/in` and `/ref`, writes only `/out` and `/tmp`; no network; non-root; identical under Docker and Apptainer; exit 0 success, 10 invalid input with `error.json`, anything else internal failure; writes `provenance.json`.
- Build on the SDK (`steps/sdk/`, package `stroke_step_sdk`) and copy `steps/template/`; follow `steps/README.md`.
- Pipelines (`pipeline.yaml`) wire typed outputs to inputs through slots; images are pinned by digest.
- Reference data and model weights are bundles (`bundle.yaml`) mounted at `/ref`, never committed. Test data: `fixtures/` (synthetic) or public data with a redistributable licence.
- Wrapping an existing tool: do not change its algorithm; check every exit code.
- Contract changes need a new ADR, a version bump and owner approval; `contracts/` changes need Opus review.
- Check: `python contracts/validate.py ...`, `make test-steps` (own virtualenv), reference tests with `stroke-step-reftest`; CI workflow `.github/workflows/steps.yml`.
