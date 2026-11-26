# Security

## Implemented In V0

- Validation of allowed extensions.
- Configurable maximum size.
- File name sanitisation.
- Basic path traversal prevention.
- `.env.example` without real secrets.
- `.env` ignored by Git.
- Environment variables documented in `docs/configuration.md`.
- Technical logs separated from user-facing messages.
- No patient-identifying data in fixtures.
- Local login with signed JWT and configurable expiry.
- Password hashing with PBKDF2-HMAC-SHA256 and random salt.
- Initial roles `admin` and `researcher`.
- Per-study owner through `owner_user_id`.
- Protection of study and download endpoints.
- Audit of login, upload and download with the acting user where applicable.
- Logs visible in the GUI with truncation.
- Reverse proxy security headers: `Content-Security-Policy` (same-origin scripts and styles, no framing), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` (share-link tokens are never sent as referrer). The mocked Playwright job runs the built frontend under the same CSP and fails on any violation.
- Uploads over `MAX_UPLOAD_SIZE_MB` are stopped while they are written (legacy and v1 uploads), nothing over the limit is kept.
- The API trusts `X-Forwarded-For` only from the Compose network, so audited client addresses cannot be spoofed by clients.
- Legacy processors receive an allowlisted environment, never the worker's secrets.
- Optional Redis password (`REDIS_PASSWORD`); direct API, frontend and Mailpit ports are published on the host's loopback only.
- Cancellation of queued or running jobs, retry of failed ones and controlled deletion with minimal audit.
- Role-protected admin dashboard.
- Temporary revocable links to share only technical PDFs, with a hashed opaque token and download auditing.

Extensions accepted by default: `.nii`, `.nii.gz`, `.dcm`, `.zip`, `.tar`, `.tar.gz`, `.gz`, `.json` and `.txt`.

The list is configured through `ALLOWED_EXTENSIONS` and must be kept aligned with `backend/app/core/config.py` and `docker-compose.yml`.

## Assumptions

- Initial use with anonymised data.
- Local or controlled deployment for the prototype, not exposed as a hardened hospital system.

## Security Roadmap

Local identity, basic permissions, job traceability, admin dashboard, local backup/restore via CLI and temporary PDF sharing are already implemented as a prototype baseline. The next security improvements must focus on real exposure and the data lifecycle: retention, TLS, host/container hardening and a more robust session policy if the system is shared outside a controlled environment.

### Local Authentication And Roles

Implemented decisions:

- Local login as the first option.
- Users created by an admin; no open public registration.
- Initial roles `admin` and `researcher`.
- Association of each study with an owner through `owner_user_id`.
- Protection of the upload, list, detail, status and download endpoints.
- Audit of login, upload and download.

Minimum controls:

- Do not store passwords in clear text.
- Use robust hashing.
- Do not return login errors that differentiate whether the email exists.
- Use JWT with expiry.
- Set a custom `AUTH_SECRET_KEY` of at least 32 characters.
- The API does not start, in any environment, if `AUTH_SECRET_KEY` keeps the placeholder value or is too short, unless `ALLOW_INSECURE_DEV_KEY=1` (local development and tests only).
- Login is rate limited per client address by the reverse proxy and throttled per account by the API (`LOGIN_MAX_FAILED_ATTEMPTS`, `LOGIN_FAILURE_WINDOW_MINUTES`); unknown addresses cost the same password hashing as known ones.
- The GUI stores the access token in `localStorage` for a simple prototype implementation. Before exposing the system in a shared environment, evaluate an `HttpOnly/Secure/SameSite` cookie, a shorter TTL and token revocation.

Expected permissions:

- `admin`: sees all studies, manages users, accesses the dashboard and can operate on jobs according to the defined policy.
- `researcher`: sees and downloads only their own studies; can cancel or delete their own if the policy allows it.

### Future Integrations

- Google/OIDC remains a future improvement for institutional users and must be able to coexist with local login.
- ORCID remains a future improvement if research use gains weight.
- Restriction by institutional domain must be configured per environment, not hardcoded.
- A complete `viewer` role is outside the current implementation; the implemented sharing uses temporary revocable links for technical PDFs.
- 2FA is out of scope for now.

### Data And Downloads

- Share links use random opaque tokens, configurable expiry and revocation.
- Downloads through a token record audit events and do not expose the ZIP or logs.
- Emails must send links, not heavy attachments.
- Logs visible in the GUI must be truncated and avoid sensitive internal paths.
- Study deletion applies soft delete and physical deletion, keeping a minimal audit record.

## Pending Before A Hospital Environment

- Real TLS.
- Reinforced auditing.
- Retention policy.
- Validated DICOM anonymisation.
- Host and container hardening.
- Regulatory and clinical evaluation.
