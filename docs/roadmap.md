# Roadmap

> **Superseded.** This phase-based roadmap is superseded by [`plan/IMPLEMENTATION_PLAN.md`](plan/IMPLEMENTATION_PLAN.md), which is now the single source of truth for planned work. This document is kept as a historical record of the prototype's phases.

This roadmap orders the functional evolution of the platform from the current state of the prototype towards a realistic base for future research or hospital use. It does not imply that the phases are implemented: it documents priority, dependencies, risks and acceptance criteria before building new capabilities.

## Project State

The current platform supports local login, basic `admin`/`researcher` roles, a per-study owner, upload of a T1w `.nii.gz`, BIDS preparation, enqueueing with Celery/Redis, a `compneuro` worker, NIfTI-to-PNG rendering with FSL `slicer`, technical artefacts, result download according to permissions, basic job management, an operational dashboard for admins, local backup/restore via CLI, temporary sharing of technical PDFs, internal/email notifications and basic per-user quotas.

There are not yet flows selectable from the GUI, an advanced admin retention screen or a formal clinical review with reviewer, date and comments.

## Functional Evolution Roadmap

### Phase 0 — Consolidated Current State

Available functionality:

- Manual upload of a T1w `.nii.gz`.
- BIDS preparation for one subject per study.
- Asynchronous processing with Celery/Redis.
- Worker based on `compneuro-anatproc`.
- PNG rendering from NIfTI.
- Technical PDF and results ZIP.
- Result download from the GUI.
- Basic audit associated with the study.

Acceptance criteria already covered:

- The API does not run long processing inside the HTTP cycle.
- `processor_adapter` remains the boundary with the external processor.
- Studies are traced in PostgreSQL and the local filesystem.
- `make test`, `make lint`, `make check-docs` and `make check-secrets` are the project's main controls.

### Phase 1 — Basic Multi-User Support

Status: implemented as a functional base.

Goal: introduce identity, study ownership and minimum permissions without changing the processing flow.

Implemented scope:

- Local login.
- Users created by an admin; no open public registration.
- Initial roles `admin` and `researcher`.
- Secure session with expiry.
- Protection of sensitive endpoints.
- `owner_user_id` on `Study`.
- Per-user study history.
- Migration of existing studies to the `system` or `admin` user.
- Basic audit of login, upload, download and delete.

Dependencies:

- `User` model and Alembic migration.
- Robust password hashing.
- Documented decision between cookie session or simple JWT.
- Definition of permissions per endpoint before touching the GUI.

Risks:

- Leakage of studies if some endpoint does not filter by owner.
- Incomplete migration of previous studies.
- Badly configured browser sessions if JWT is chosen without a clear expiry and storage policy.
- Mixing auth with processing logic instead of keeping it in the API layer.

Acceptance criteria:

- A working local login exists.
- At least one initial admin user exists.
- Studies have an owner.
- `researcher` only sees their own studies.
- `admin` sees all studies.
- Sensitive endpoints are protected.
- The GUI shows per-user history.
- The documentation explains user creation, roles and permissions.
- Basic permission tests pass.
- Technical compatibility with the `compneuro` flow is kept.

### Phase 2 — Job Management And Traceability

Status: implemented as a functional base.

Goal: improve operational control of jobs without yet introducing advanced administration.

Implemented scope:

- More detailed job states, without fake percentages.
- Logs visible in the GUI with truncation.
- Final logs downloadable or viewable safely.
- Cancellation of queued jobs and cancellation requests for running processing.
- Retry of failed jobs.
- Soft delete in the database.
- Controlled physical deletion of input, BIDS, output, PNG, PDF, ZIP and logs.
- Extended audit.
- Job detail view.

Risks:

- Exposing sensitive internal paths in logs.
- Breaking traceability if physical deletion removes all operational evidence.
- Non-idempotent retries if partial results are not cleaned up.

Acceptance criteria:

- The owner and the admin can view job details according to permissions.
- Logs are shown truncated and without unnecessary internal paths.
- Queued jobs can be cancelled before they start running and running jobs can receive a traceable termination request.
- Deletion keeps a minimal audit record.
- Retrying a failed job leaves traceability of the previous attempt.

### Phase 3 — Admin Dashboard

Goal: give the administrator minimum operational visibility.

Implemented status: basic dashboard accessible only to `admin` through the GUI and `GET /api/admin/dashboard`.

Implemented scope:

- Queue state.
- Active and failed jobs.
- Disk usage.
- Healthchecks.
- Worker, Redis and PostgreSQL state.
- Users.
- Studies by status.
- Basic alerts.

Dependencies:

- Phase 1 for the admin role.
- Phase 2 for more useful states and traceability.

Acceptance criteria:

- The dashboard is only accessible to admins.
- It shows global data without manually querying the database.
- Alerts do not block processing.

### Phase 4 — Backups And Maintenance

Goal: document and automate a reasonable local recovery for the prototype without complex external infrastructure.

Status: implemented as a CLI operation through `make backup` and `make restore`, without endpoints or GUI.

Implemented scope:

- Local PostgreSQL backup script.
- Local backup script for `data/studies`.
- Documented restore script.
- Smoke test after restore.
- Makefile commands.
- Operations documentation.
- Manual maintenance policy.

Decisions covered:

- The restorable unit is database + `data/studies`.
- Backups are stored by default in `backups/`, outside Git, and must be copied off the server for real protection against disk loss.

Acceptance criteria:

- An operator can back up and restore by following the documentation.
- After a restore, `make smoke` or an equivalent check confirms that the API responds.
- The documentation warns that the DB and the filesystem must be restored together.

### Phase 5 — Secure Report Sharing

Goal: share results without creating full accounts for external recipients.

Status: implemented for technical PDF download through a temporary opaque token, with expiry, revocation and auditing. It does not share the ZIP or logs.

Recommended scope:

- Temporary opaque links.
- Configurable expiry.
- Revocation.
- Access audit.
- PDF download through a token.
- Optional ZIP download if the policy allows it in a later phase.
- External viewer without a full account or a future `viewer` role is out of scope.

Dependencies:

- Phase 1 for owner and permissions.
- Phase 2 for extended audit.

Risks:

- Tokens with too long a lifetime.
- Non-revocable links.
- Sharing the ZIP with more data than necessary.

Acceptance criteria:

- The owner or admin can create and revoke links.
- Links expire.
- Every download is audited.
- The token endpoint does not allow enumerating studies.

### Phase 6 — Notifications

Goal: notify about completion or failure without attaching heavy data.

Status: implemented with internal notifications, per-user preferences and configurable SMTP; Docker Compose uses a local Mailpit by default.

Implemented scope:

- Configurable SMTP.
- Email on completion or failure.
- Links only; no PDF, ZIP or heavy data attached.
- Notification preferences.
- Notification to admins on critical errors.
- Internal notifications in the UI.

Dependencies:

- Phase 1 for the user's institutional email.
- Phase 5 if emails link to temporary shared resources.

Acceptance criteria:

- The system does not send heavy attachments.
- SMTP errors do not mark the main processing as failed.
- Notifications are recorded for diagnostics.

### Phase 7 — Multiple Upload And Batches

Goal: allow uploading several studies without changing the per-file processing interface.

Recommended scope:

- Multiple upload of `.nii.gz`.
- One job per file.
- Optional `BatchUpload` entity to group the batch.
- Filename -> subject ID table.
- Partial result if some fail.
- Optional aggregated download.

Recommended decision:

- Keep one job per file. The batch groups traceability and user experience; it does not merge processing.

Risks:

- A partial failure blocking the whole batch.
- Saturating disk or queue without limits.
- Ambiguity between file name and BIDS subject.

Acceptance criteria:

- Each file produces an independent study/job.
- The user can see success/failure per file.
- The batch does not hide individual errors.

### Phase 8 — Retention, Quotas And Storage Control

Goal: control data growth and operating costs.

Status: basic per-user quota implemented; initial retention via CLI/configuration implemented with `keep_forever`, dry run, audit and physical deletion, but disabled by default for real execution. An advanced admin screen and guided manual cleanup are still pending.

Recommended scope:

- Retention policy via CLI/configuration.
- Initial recommendation: 90 days.
- `keep_forever` flag.
- Dry run by default.
- Audit of automatic deletion.
- Per-user quota evolution already implemented as a basic upload limit.
- Disk usage alert.
- Controlled manual cleanup.

Dependencies:

- Phase 1 for per-user quotas.
- Phase 2 for safe deletion and audit.
- Phase 3 for disk alerts.

Acceptance criteria:

- The dry run shows what would be deleted without removing data.
- `keep_forever` excludes studies from automatic retention.
- Every automatic deletion generates an audit event.

### Phase 9 — Configurable Flows

Goal: prepare the selection of processing flows without turning the platform into a complex dynamic tool manager.

Recommended scope:

- Selection of the processing flow per upload.
- Recording of `pipeline_name` and `pipeline_version`.
- Enabling/disabling flows by configuration.
- Keep `compneuro` as the main flow.
- Keep `dummy` as the development flow.
- Advanced tool management only as a distant future.

Recommended decision:

- Prepare the model/configuration before an advanced UI.
- Keep using `processor_adapter` as the mandatory boundary.

Risks:

- Coupling the API or worker to the internal details of each flow.
- Allowing unvalidated combinations of inputs and flows.

Acceptance criteria:

- Each study records the flow used and its version.
- The selection does not break the current compneuro flow.
- Disabled flows do not appear in the GUI.

### Phase 10 — Institutional Integration

Goal: prepare the system for institutional users and researchers with reinforced security.

Recommended scope:

- Google login/OIDC.
- Restriction by institutional domain.
- ORCID login for researchers.
- DICOM anonymisation check.
- Preparation for real data.
- Deployment hardening.
- Real TLS.
- Reinforced auditing.

Recommended decision:

- Local login first, OIDC later. Both must be able to coexist.
- Google/OIDC is desirable for an institutional environment.
- ORCID only gains priority if research use weighs more than hospital use.

Risks:

- Complexity of external identity before having solid internal permissions.
- Confusing an anonymisation check with guaranteed anonymisation.

Acceptance criteria:

- External providers are linked to internal users.
- The domain restriction is configured outside the code.
- Auditing distinguishes local login from external login.

### Phase 11 — Clinical Review

Goal: separate technical results from an eventual professional validation.

Recommended scope:

- `technical_only` flag.
- `reviewed` state.
- `validated` state.
- Reviewer user.
- Review date.
- Comments.

Dependencies:

- Phase 1 for reviewer identity.
- Phase 2 for traceability.
- Clinical/legal definition external to software development.

Acceptance criteria:

- Only authorised users can mark review or validation.
- The review does not modify original results.
- The change history is audited.

## Recommended Prioritisation

The optimal next phase is **Phase 7 — Multiple Upload And Batches**.

Rationale:

- Phase 1 already establishes identity, roles, per-study owner and basic history.
- Phase 2 already adds job detail, truncated logs, cancellation of queued or running jobs, retry and safe deletion.
- Phase 3 already provides global operational visibility for admins.
- Local backup and restore are already covered as a CLI operation before moving on to batches, retention or institutional integration.
- Phase 5 already allows sharing technical PDFs with temporary, revocable and audited links.

Optimal implementation order after closing this roadmap:

1. Phase 7: multiple upload and batches.
2. Phase 8: retention, quotas and storage.
3. Phase 9: configurable flows.
4. Phase 10: institutional integration.
5. Phase 11: clinical review.

## Implemented History

Phases 1, 2, 3, 4, 5 and 6 are already implemented as a functional and operational base:

- Phase 1: local login, `admin`/`researcher` roles, per-study owner, user creation by admins and permissions per endpoint.
- Phase 2: job detail, truncated logs, cancellation of queued or running jobs, retry of failed ones, soft delete, controlled physical deletion and minimal audit.
- Phase 3: admin dashboard with queue, active/failed jobs, disk usage, healthchecks, users, studies by status and non-blocking alerts.
- Phase 4: local backup/restore via CLI of PostgreSQL and `data/studies`, with strong confirmation for restore and a subsequent smoke test.
- Phase 5: temporary, revocable links to download technical PDFs without a full account, with token hashing and access audit.
- Phase 6: internal notifications, per-user preferences and configurable SMTP email.

Living technical details are in `docs/architecture.md`, `docs/api.md`, `docs/developer-manual.md` and the tests.

## Do Not Implement Yet

- Google login.
- ORCID login.
- Complete `viewer` role.
- ZIP sharing by link.
- Advanced institutional email.
- Automatic retention.
- Multiple upload.
- Advanced pipeline manager.
- 2FA.
- Advanced retention with `keep_forever`, dry run and automatic cleanup.
- Real DICOM anonymisation.

## Pending Decisions

- Evaluate whether to migrate from JWT in localStorage to an `HttpOnly` cookie before real exposure.
- Define whether the textual `AuditEvent.actor` is kept for compatibility or removed in a future migration.
- Decide whether the `system` user should be hidden from the admin dashboard.
- Define a retention policy before using sensitive data.
