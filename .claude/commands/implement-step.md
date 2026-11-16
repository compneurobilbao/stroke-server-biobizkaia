---
description: Build or wrap a processing tool as a step under the step contract, with the SDK and template.
---

# Implement a step

Step or card: $ARGUMENTS

1. Read `docs/contracts/step-contract.md`, `contracts/artefact-types.yaml` and `steps/README.md`.
2. Start from `steps/template/` (copy to `steps/<step-name>/`, or to the `step/` folder of the tool's own repository when the card says so) and follow `steps/README.md`: `step.yaml`, `run(ctx)` in `src/<package>/step.py`, `Dockerfile`, `Apptainer.def`, reference dataset generator and tests.
3. Inputs and outputs use declared artefact types; parameters have defaults and a schema; invalid input raises a declared error code (exit 10 with `error.json`); the SDK writes `provenance.json`.
4. Wrapping an existing tool: do not change its algorithm; check every subprocess exit code.
5. Reference datasets are synthetic or public with a redistributable licence; reference data and weights are bundles, never committed.
6. Add the step's container job to `.github/workflows/steps.yml` only if the card's "Touches" list includes it; otherwise propose it as a follow-up.
7. Check: `python contracts/validate.py --kind step steps/<step-name>/step.yaml`, `make test-steps` (own virtualenv), and the Docker/Apptainer reference test from `steps/README.md` where available. CI is the source of truth.
