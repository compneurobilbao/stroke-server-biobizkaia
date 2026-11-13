# Demonstration Guide For The Thesis Committee (Historical Note)

> Historical note: this was the live-demo script for the master's thesis defence of the prototype. It is kept for reference only; the thesis framing no longer applies to the project.

This guide was meant as operational support during the defence. It does not replace the thesis report: it orders what to show, what to say and what to avoid in order to demonstrate the system live without depending on a long processing run finishing during the session.

## Goal Of The Demo

Demonstrate that the project is not just an upload interface but a traceable platform for asynchronous neuroimaging processing:

- Web GUI for research users and administration.
- FastAPI API with permissions, validation and traceability.
- Celery/Redis queue for long jobs outside the HTTP cycle.
- `processor_adapter` boundary with the external processor.
- Persistence in PostgreSQL and `data/studies`.
- Technical artefacts: PDF, rendered PNGs and ZIP.
- Operation: dashboard, logs, backup, retention via CLI and notifications.

Central message:

> The value of the work lies in integrating, isolating and operating an external pipeline in a reproducible and traceable way, not in reimplementing a clinical algorithm.

## Limits Worth Clarifying

- Do not use real or identifying data during the demo.
- The generated PDF is technical, not clinical.
- The platform is not a certified medical product.
- The `Technical only` / `Reviewed` / `Validated` review is internal traceability, not formal clinical validation.
- Retention has a dry run available, but real deletion is disabled by default with `RETENTION_ENABLED=false`.
- The real run with `compneuro` may take too long for a defence; it is advisable to have an already completed study.

## Preparation

Run before the session:

```bash
make up
make migrate
make smoke
```

Check access:

- GUI: `http://localhost`
- Swagger/OpenAPI: `http://localhost/api/docs`
- Mailpit: `http://localhost:8025`

Prepare data and users:

- A working `admin` user.
- A working `researcher` user.
- An anonymised test `.nii.gz` file.
- An already completed study with the technical PDF available.
- A failed or cancellable study if retry/cancellation is to be shown.
- Mailpit open if local emails are to be shown.

If the initial admin is missing:

```bash
make create-admin EMAIL=admin@example.org
```

## Short Script (10-15 Minutes)

### 1. Opening

Brief explanation:

> This platform allows a researcher to upload an anonymised T1w anatomical study, send it to a processing queue, isolate the execution of the external pipeline, record traceability and download technical results. Administration can supervise users, operational state, storage and maintenance.

Quickly show:

- GUI.
- Swagger.
- Admin dashboard if already open in another tab.

### 2. Login And Role Separation

Log in as `researcher`.

Show:

- Local login.
- Own study history.
- Actions available to the researcher.

Useful phrase:

> Studies have an owner. A researcher works on their own history, while administration has a global view.

Then log in as `admin` or keep that for the administration block.

### 3. Study Upload

As `researcher`, upload an anonymised `.nii.gz`.

Show:

- File selection.
- BIDS identifier, for example `sub-O01`.
- Submission for processing.
- Initial state `queued` or `processing`.

Explanation:

> The API validates permissions, extension and quota, sanitises the name, stores the file, creates a `Study`, creates a `ProcessingJob`, records an audit event and enqueues a Celery task. The HTTP request is not blocked by the processing.

### 4. Queue And Asynchronous Processing

Show the state change or a study already processing.

Explanation:

> Redis acts as the queue broker. Celery runs the job in a separate worker. The worker calls `processor_adapter`, which is the mandatory boundary with the external processor.

Key phrase:

> FastAPI does not know the internal details of the algorithm. It only coordinates traceability, permissions and artefacts.

### 5. Technical Detail, Logs And Jobs

Open the detail of a study.

Show:

- Study data.
- List of jobs.
- Truncated logs.
- Final state or error.
- Retry if the study is failed.
- Cancellation if the study is queued or processing.

Explanation:

> Traceability makes it possible to know what was attempted, when, with what state and which logs it produced. This is essential for reproducible research and maintenance.

### 6. Output Artefacts

Open a completed study.

Show:

- Technical PDF.
- Results ZIP if available.
- PNGs rendered from NIfTI.

Explanation:

> The PDF summarises technical artefacts and processing metadata. It does not interpret the image or give a diagnosis.

### 7. Technical Review

Show the change of review state if available.

States:

- `Technical only`.
- `Reviewed`.
- `Validated`.

Explanation:

> This mark does not replace formal clinical review. It serves for internal traceability of the result within the platform.

### 8. Temporary PDF Sharing

On a completed study, create a temporary link.

Show:

- Creation of the link.
- Opening it in a private window or a new tab.
- Downloading the PDF without logging in.
- Revocation if desired.

Explanation:

> Sharing uses temporary opaque tokens. It only shares the technical PDF, not the ZIP or the logs.

### 9. Notifications

Show:

- Internal notifications in the GUI.
- Notification preferences if visible.
- Mailpit at `http://localhost:8025`.

Explanation:

> Emails do not attach heavy files or clinical data. They report the result and link to the platform. If SMTP fails, the main processing is not marked as failed.

### 10. Admin Dashboard

Log in as `admin`.

Show:

- Queue state.
- Active and failed jobs.
- State of PostgreSQL, Redis and worker.
- Disk usage.
- Users.
- Studies by status.
- Basic alerts.

Explanation:

> The dashboard gives operational visibility without turning the admin panel into a place from which to run pipelines manually.

### 11. User And Quota Management

From admin, show:

- User creation.
- `admin` or `researcher` role.
- Storage quota.
- Activation/deactivation or soft deletion if a test user is used.

Explanation:

> Quotas and roles prepare the platform for more controlled shared use, although without yet entering OIDC/ORCID institutional identity.

### 12. Documented API

Open `http://localhost/api/docs`.

Show:

- Main endpoints.
- Input/output schemas.
- Protected endpoints.

Explanation:

> The API is documented automatically with OpenAPI. This helps validate contracts and eases maintenance or future integration.

### 13. Operation: Backup And Retention

Show commands, do not run a restore live.

```bash
make backup
make retention-dry-run DAYS=90
```

Explanation:

> The backup treats PostgreSQL and `data/studies` as a logical unit. Retention is tried first in dry-run mode. Real deletion requires explicit activation with `RETENTION_ENABLED=true`.

Do not run live unless on a disposable environment:

```bash
make restore BACKUP_DIR=backups/<timestamp> CONFIRM_RESTORE=YES_I_UNDERSTAND
make retention-execute DAYS=90
make clean
```

## Plan B If Something Fails

If processing does not finish:

- Use an already completed study.
- Explain that the architectural value lies in the asynchronous queue and traceability.
- Show logs and job state.

If Docker is slow:

- Show Swagger, the dashboard and a precomputed study.
- Avoid rebuilds or long commands.

If Mailpit does not show an email:

- Show internal notifications.
- Explain that SMTP is auxiliary and does not block processing.

If the test file fails:

- Show the `failed` state.
- Open the logs.
- Show retry.
- Use that situation as an example of incident traceability.

If asked about clinical diagnosis:

> The system does not diagnose. It generates traceable technical processing artefacts. Formal clinical validation is outside the scope of the thesis and would require professionals, a protocol and a regulatory analysis.

## Likely Committee Questions

### Why use Celery and Redis?

Because processing can take minutes or hours. Taking it out of the HTTP cycle avoids timeouts and allows retries, cancellation, traceability and scaling of workers.

### Why does `processor_adapter` exist?

To decouple the platform from the external processor. The API and the worker do not depend on clinical details or specific scripts.

### Why local filesystem and not S3/MinIO?

For the scope of the thesis it reduces operational complexity. The design documents that limitation and leaves S3/MinIO as a future evolution.

### What about sensitive data?

The version assumes anonymised studies. Identifying data must not be used. The system adds permissions, traceability and operational retention, but does not replace a complete institutional security policy.

### Why is retention not in the GUI?

Because deleting data is a sensitive operation. It was left to CLI/configuration, with a dry run and real deletion disabled by default, to avoid accidental executions from a web screen.

### How would another pipeline be integrated?

By changing or extending the configuration and the adapter, keeping the boundary in `processor_adapter` and without coupling FastAPI to the algorithm.

### What does it add compared with running the script manually?

Study ownership, asynchronous queue, audit, logs, downloadable artefacts, permissions, notifications, dashboard, backup, retention and operational documentation.

## Quick Checklist Before Starting

- Services up with `make up`.
- Migrations applied with `make migrate`.
- `make smoke` passes.
- Admin and researcher created.
- Completed study prepared.
- Anonymised `.nii.gz` file ready.
- Mailpit open if email is shown.
- Swagger open.
- No need to run `make clean`, `restore` or a long processing run live.

## Recommended Closing

Final phrase:

> The platform demonstrates a transferable base for integrating neuroimaging processing in a traceable web environment: it separates responsibilities, avoids blocking the API, keeps an audit trail, protects access by roles and documents clearly what is implemented and what is left for a later phase.
