---
name: security
description: Guide for security-sensitive work (auth, uploads, file handling, secrets, step isolation, data protection). Use when changing auth, uploads, storage, executors or configuration.
---

# Security

- Secrets: never read `.env` or credential files; nothing secret in the repository; `make check-secrets` and gitleaks (CI, pre-commit).
- Uploads and files: type sniffing, size limits, safe paths (no traversal), storage only through the storage service.
- Auth: roles, ownership and quotas enforced server-side; the baseline before public exposure is D13 in the plan (cookie sessions with CSRF, OIDC, TLS, rate limiting). Auth changes need Opus review.
- Steps run without network, as non-root, with read-only `/in` and `/ref`; the `docker` executor only starts allow-listed images.
- No patient data anywhere; de-identification and sealing code need Opus review.
- Errors and logs do not expose stack traces, internal paths or personal data.
- See `docs/security.md` for the current state.
