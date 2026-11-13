# User Manual

The GUI is currently in Spanish. Button and status labels are given below in English with the on-screen Spanish label in parentheses; an English interface is planned.

1. Open the GUI.
2. Log in with your email and password.
3. Confirm that the image is anonymised.
4. Select a T1w anatomical image in `.nii.gz` format.
5. Enter a BIDS subject identifier, for example `sub-O01`. If you leave it empty, the system generates a safe one.
6. Press "Send to processing" (`Enviar a procesamiento`).
7. Wait for the status to go from "Queued" (`En cola`) to "Processing" (`Procesando`) and then to "Completed" (`Completado`). It can take between 10 minutes and 1 hour.
8. Download the technical PDF and, if it appears, the results ZIP.
9. If you need to share the PDF with someone without an account, use "Share" (`Compartir`) to create a temporary, revocable link.

If the status is "Failed" (`Fallido`), notify the administrator and do not use that result.

The technical PDF lists metadata, generated files and PNGs rendered automatically from NIfTI results with FSL `slicer`. It is not a validated clinical report and must not be used to make medical decisions without professional review.

If the status appears as completed with warnings, the main processing finished but some NIfTI file may not have been rendered to PNG. In that case check the PDF and notify the administrator if expected images are missing.

## Study Management

From the studies table you can, depending on your role and the study status:

- View the technical detail of the study and its jobs.
- View truncated logs if they exist.
- Cancel queued studies or request cancellation if they are already processing, when the action is available in the GUI.
- Retry failed studies.
- Delete studies that are not processing.
- Share the technical PDF of completed studies through temporary links.
- Mark results as "Technical only" (`Solo técnico`), or as "Reviewed" (`Revisado`) / "Validated" (`Validado`) when completed, for internal traceability.

Deletion physically removes the associated files and leaves a minimal audit record in the database.

## Report Sharing

On "Completed" studies with an available PDF, the "Share" button lets you create a temporary link. That link allows downloading only the technical PDF, not the ZIP or the logs, and does not require the recipient to create an account.

Links expire automatically and can be revoked from the same screen. If an already created link is lost, it cannot be recovered because the system does not store the token in clear text; create a new one and revoke the previous one if appropriate.

## Users And Roles

- `researcher`: can upload studies from the GUI, see their own history, check their quota, download their own results, manage PDF links of their studies and mark technical review of their results.
- `admin`: can see all studies, create/deactivate users, define quotas, view the operational dashboard, delete permitted studies, manage PDF links and mark technical review. The admin panel exposes no upload, cancel/retry or pipeline execution.

There is no open public registration. Users are created from an admin account.

## Basic Administration

To create the first admin, use:

```bash
make create-admin EMAIL=admin@example.org
```

After that, the admin can create `researcher` or `admin` users from the GUI, assign storage quotas and deactivate/soft-delete users.

The admin dashboard shows the queue, failed jobs, services, disk usage, users and studies by status. It is meant for operational diagnosis; it does not replace reviewing logs if a processing run fails.

Multiple upload, a GUI retention screen and formal clinical review are left for later phases.
