# ADR-0008: SP2 results enter through one schema, as a step or through an endpoint

- Status: accepted
- Date: 2026-11-13
- Plan reference: decision D8, cards F0-10, epics E2-5 and E3-2

## Context

The partner modelling group (SP2) develops a simulator that predicts rehabilitation routes from lesion-derived features. Its code may be packaged for us to run, or it may run only at the partner site. Platform work must not wait for SP2, and no identifiable data may leave the platform.

## Decision

1. SP2 output is one versioned schema, `sp2-result/1.x`: simulated features, ranked routes with predicted gain, burden and uncertainty, model version and lock flag. It becomes an artefact type in the registry (format `json`, with that schema) when the schema is written (epic E2-5).
2. Path 1 (preferred): the SP2 simulator is packaged as a step under the step contract (ADR-0001) that provides a slot (provisionally `sp2-routes`) consuming the MNI lesion mask and disconnection artefacts and producing the `sp2-result` artefact. It runs on the HPC or on-premise like any other step; its model is a bundle (ADR-0006).
3. Path 2: a service-account endpoint `PUT /api/v1/cases/{id}/sp2-results` accepts results computed at the partner site, and `GET /api/v1/cases/{id}/sp2-inputs` returns only derived, non-identifiable inputs (MNI lesion mask, disconnection features, permitted baseline variables). An uploaded result is recorded in the run provenance as a step record with `executor: external` and `runtime: none` ([provenance.md §3.2](../contracts/provenance.md#32-step-records)), validated against the same schema.
4. A stub implementation of the slot and of the endpoint exists from the first version.

## Consequences

- The report and the UI consume SP2 results without knowing which path produced them.
- Contract 1.0 already reserves `executor: external`, so path 2 needs no contract change; the `sp2-result` schema and slot are added later as additive changes (new artefact type, new optional slot).
- The partner can test its image against a conformance suite built from `contracts/validate.py` and the reference-test mechanism.

## Alternatives considered

- **Endpoint only.** Rejected as the main path: results would depend on a remote service at run time and could not be reproduced on our infrastructure or on-premise.
- **Step only.** Rejected as the only path: the partner's code may not be packageable or shareable early, and the project must not block on it.
- **Two different result formats for the two paths.** Rejected: duplicates validation, report code and provenance handling.
- **Sending images to the partner.** Rejected: only derived, non-identifiable inputs leave the platform.
