# Technical Summary (Historical Note)

> Historical note: this summary was written for the master's thesis prototype and is kept for reference only. The current plan is `plan/IMPLEMENTATION_PLAN.md`.

This project implements a containerised web platform that integrates batch neuroimaging processing with automatic generation of technical PDF summaries and result download.

The main contribution is not the processing algorithm but the software layer that allows it to be operated in a controlled, traceable and decoupled way. The architecture separates interface, API, persistence, queue, worker and CLI adapter, reducing coupling and making a future replacement of the processor easier.

The integration with `compneuro-anatproc` automatically prepares BIDS for a T1w `.nii.gz`, runs the anatomical flow from a worker based on its Docker image, validates `Preproc/BET` and `Preproc/ProbTissue` results, renders NIfTI to PNG with FSL `slicer` and generates the platform's own technical artefacts. The resulting PDF summarises the generated results and performs no clinical interpretation. The system is not yet a complete medical product and requires clinical validation, institutional authentication or a hardened session policy, hardening and data policies before real hospital use.

The key decision is to keep a lightweight API and a specialised worker: FastAPI does not process neuroimaging, Celery runs long tasks and `processor_adapter` encapsulates the external flow.

## Planned Evolution

The roadmap proposes an incremental evolution: first identity and permissions, then operational traceability, admin dashboard, local backups/restore, secure sharing, notifications, batches, retention, configurable flows, institutional integration and clinical review.

The platform includes a first multi-user layer with local login, `admin`/`researcher` roles, an owner per study, endpoint protection, basic per-user quotas and temporary sharing of technical PDFs. This unlocks per-user history, access control, real auditing, administration and controlled delivery of reports without modifying the boundary with the external processor. Integrations such as Google/OIDC, ORCID, advanced institutional email, automatic retention or formal clinical review are justified as later phases because they depend on prior identity, permissions and traceability.
