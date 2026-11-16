---
name: qa
description: Guide for tests and test data (pytest, step reference tests, Vitest, synthetic fixtures, make e2e). Use when adding or changing tests.
---

# QA

- `docs/testing.md` lists the tests and how to run them. Backend/worker/executor/contract/fixture tests in `tests/` (`make test`); step SDK and step tests in `steps/` (`make test-steps`, own virtualenv); frontend tests with Vitest (`npm test`); PostgreSQL-marked tests run in CI.
- Test data: synthetic fixtures from `fixtures/` (`python -m fixtures.generate`, deterministic) or public de-identified data with a redistributable licence. Never patient data. `make e2e` uses a public CC0 OpenNeuro T1w on a running instance with a dedicated test account.
- Test what has value: API flows and permission boundaries, validation, cancellation, failure and timeout paths, contract compliance, golden files for reports.
- Avoid fragile suites; tests must be deterministic (fixed seeds).
- CI is the source of truth; report real results only.
