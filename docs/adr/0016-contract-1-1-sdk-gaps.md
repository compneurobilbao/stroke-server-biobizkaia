# ADR-0016: Contract 1.1 closes the gaps found while building the step SDK

- Status: proposed
- Date: 2026-11-13
- Plan reference: decision D1, cards F0-10 and F0-21 (plan section 7: contract changes need a new ADR and a version bump)

## Context

Building the step SDK and the template step (card F0-21) on contract 1.0 ([ADR-0001](0001-step-contract.md)) exposed six places where the specification was silent or self-contradictory:

1. The SDK must reject a missing required input, an input that fails its artefact-type checks and invalid parameters with exit 10 (step-contract 2.4 item 5), but 4.3 requires every exit-10 code to be declared by the step, and no code was defined for these conditions. Every manifest had to repeat three boilerplate entries.
2. `contracts/validate.py` checked `provenance.parameters` against the parameter schema for every status, so the honest record of a run rejected for invalid parameters could never validate.
3. Nothing said what `provenance.outputs[]` holds, or which files may stay in `/out`, after a rejection or failure.
4. 2.4 item 6 asked steps to stop promptly on `SIGTERM` but fixed neither the exit code nor whether a report is written.
5. 1.7 required fixtures to be paths in the repository, but did not say how a reference test may use generated, uncommitted fixtures (large binary NIfTI files on the working grid).
6. Provenance `software[]` was not stated to be free-form or to need specific entries.

## Decision

The contract becomes version `1.1` ([versioning.md §2.1](../contracts/versioning.md#21-history)):

1. Reserved error codes `input_missing`, `input_format_invalid`, `invalid_parameters` (category `input_rejected`, exit 10) and `terminated` (category `internal`). Steps declaring 1.1 declare them implicitly, must use them for these conditions, and may list them only with the reserved category ([step-contract.md §4.4](../contracts/step-contract.md#44-reserved-error-codes)).
2. `provenance.parameters` is checked against the parameter schema only for `status: succeeded`; rejected and failed records hold the parameters as far as the step read them (the raw object when they failed validation) ([provenance.md §2](../contracts/provenance.md#2-stepprovenance)).
3. With 1.1, `provenance.outputs` is empty unless `status: succeeded`, and after exit 10 `/out` holds only `error.json`, `provenance.json` and `extra/`. Outputs are only ever collected after exit 0.
4. After `SIGTERM` a step should exit with 143, after writing, if it can, an `internal` `error.json` with code `terminated` and a `failed` provenance record within the 30-second grace period; the executor's own outcome (`canceled` or `timed_out`) decides, whatever the exit code.
5. An optional `reference_test.generator` names a deterministic, synthetic-only Python script that the reference-test runner runs before staging; generated fixtures need not be committed, and expected files must be computed independently of the step ([step-contract.md §1.7](../contracts/step-contract.md#17-reference-test)).
6. `software[]` stays free-form and unchecked; SDK-based steps record `compneuro-step-sdk` and `python` automatically.

`contracts/validate.py` implements contract 1.1 (`CONTRACT_MINOR = 1`) and, when an `error.json` is given with its manifest, checks that an exit-10 code is declared or reserved with the right category.

## Version: 1.1, not a patch

`contract_version` is `MAJOR.MINOR`; the contract has no patch level, so a "1.0.1" cannot be declared by a document or checked by a component. More importantly, the change alters what components accept: a step that relies on the reserved codes without declaring them, or that uses `generator`, is invalid for a component implementing 1.0. Under the compatibility rule ([versioning.md §2.4](../contracts/versioning.md#2-the-contract-version)) such a step must declare `1.1`, so that a 1.0 component refuses it with a message naming both versions instead of failing on an undeclared code. The schema changes are additive (one optional field), so this is a minor version.

Rules that would reject documents valid under 1.0 (reserved-code categories, empty outputs on failure, `generator`) apply only to documents declaring 1.1, so every 1.0 document stays valid (versioning.md §2.3). The implicit declaration of the reserved codes also applies only to steps declaring 1.1, so that what a 1.0 step emits stays valid for 1.0 components. Skipping the parameter check on rejected and failed records applies to all records, because it only accepts more.

## Consequences

- Manifests become shorter; the UI translates the reserved codes once.
- Rejected runs produce records that validate, so the orchestrator can treat a validation failure of a rejected record as a contract violation without exceptions.
- Steps written for 1.0 keep working unchanged; the SDK applies the 1.0 rules to them (it still requires them to declare the three rejection codes).
- The reference-test runner and the nightly CI (card F1-14) must run `generator` before staging.
- The orchestrator (card F0-15) and executors (F0-13, F0-14, F0-16) implement 1.1 from the start.

## Alternatives considered

- **SDK-standard codes that every step must declare** (the SDK's interim behaviour). Rejected: three identical entries in every manifest, and nothing stops two steps from giving them different meanings.
- **Recording defaults or `{}` as parameters on rejection, keeping the check for every status.** Rejected: the record would describe a run that did not happen.
- **Listing partial outputs on rejection.** Rejected: they are never collected, and listing them invites consumers to trust them.
- **Committing generated fixtures.** Rejected for large binary fixtures; still allowed for small ones.
- **Requiring specific `software[]` entries.** Rejected: steps need not use the SDK or Python, and a schema minimum would narrow 1.0.
- **Calling the change 1.0.1 without changing `contract_version`.** Rejected for the reasons in "Version" above.
