# Architecture decision records

One file per decision, named `NNNN-short-title.md`, numbered in order of acceptance. Use `0000-template.md`.
A decision is changed by a new record that supersedes the old one; accepted records are not edited.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-step-contract.md) | Every processing tool is a step behind one contract (D1) | accepted |
| [0002](0002-declarative-pipelines-own-orchestrator.md) | Pipelines are small declarative DAGs run by our own orchestrator on Celery (D2) | accepted |
| [0003](0003-executors.md) | One executor interface with subprocess, docker and slurm backends (D3) | accepted |
| [0004](0004-data-model.md) | Case-centred data model with pipeline runs, step runs and typed artefacts (D4) | accepted |
| [0005](0005-storage.md) | Filesystem case store with BIDS raw data and per-step derivatives (D5) | accepted |
| [0006](0006-reference-bundles-are-data.md) | Reference bundles and models are versioned data, not code (D6) | accepted |
| [0007](0007-open-by-default-digests-and-stubs.md) | Open code, steps referenced by digest, stubs as fixtures (D7) | accepted |
| [0008](0008-sp2-contract.md) | SP2 results enter through one schema, as a step or through an endpoint (D8) | accepted |
| [0015](0015-selectable-implementations-per-slot.md) | Selectable implementations per pipeline slot (D15) | accepted |
| [0016](0016-contract-1-1-sdk-gaps.md) | Contract 1.1 closes the gaps found while building the step SDK | proposed |

Numbers 0001–0015 follow the plan's decision numbers D1–D15; 0009–0014 are reserved for D9–D14. Records for decisions not in the plan continue from 0016.
The normative specification that these records point to is in [docs/contracts/](../contracts/README.md).
