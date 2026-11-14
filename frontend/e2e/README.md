# End-to-end tests (Playwright)

Tests of the case wizard (both modes) and the run page.

## Mode in use: mocked API

`e2e/mockApi.ts` answers every `/api` request in the browser through Playwright route
interception, with shapes taken from `docs/api/openapi-v1.json`. Only the built frontend is
served (`npm run preview`). The tests check what the UI sends (bodies, upload paths, no exact age,
only changed implementations) and what it shows (defaults, unavailable slots, run timeline).

## Running locally

```bash
cd frontend
npm ci
npm install --no-save --no-package-lock @playwright/test@1.56.1
npx playwright install chromium
npm run build
npx playwright test
```

`@playwright/test` is installed with `--no-save` for now (see the pull request of card F0-31 for
why); the CI job `frontend-e2e` does the same with the same pinned version.

## Switching to the real stack

When the Compose `ci` stub profile is available to CI, a second Playwright project should run the
same specs without `installMockApi`: start the stack, create a test user with the backend seeding
CLI, log in through the login form, and use the synthetic NIfTI files under `fixtures/data/`
(the backend validates uploads, so the placeholder bytes used with the mock are not enough).
`PLAYWRIGHT_BASE_URL` already lets the config target a running stack.
