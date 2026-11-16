import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end tests of the case wizard and run page (cards F0-31 and F0-31a).
 *
 * Two projects:
 *
 * - `mocked` (always defined): the tests under `e2e/mocked/` intercept `/api` in the browser
 *   (`e2e/mocked/mockApi.ts`), so only the built frontend is served (`npm run build` first; the
 *   web server below serves `dist/`). Fast, no backend.
 * - `real-stack` (only when `E2E_REAL_STACK=1`): the tests under `e2e/real/` drive the Docker
 *   Compose stack (reverse proxy, API, workers, `pipelines` profile with the docker executor).
 *   They need `PLAYWRIGHT_BASE_URL` (for example `http://localhost`), `E2E_USER_EMAIL` and
 *   `E2E_USER_PASSWORD` of a throwaway test user. See `e2e/README.md` and
 *   `scripts/e2e/real_stack.sh`.
 *
 * Select one with `npx playwright test --project=mocked` or `--project=real-stack`.
 */
const realStack = process.env.E2E_REAL_STACK === '1';
if (realStack && !process.env.PLAYWRIGHT_BASE_URL) {
  throw new Error('E2E_REAL_STACK=1 needs PLAYWRIGHT_BASE_URL (the URL of the running stack)');
}
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? 'http://127.0.0.1:4173';

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL,
    locale: 'en-US',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'mocked', testDir: './e2e/mocked', use: { ...devices['Desktop Chrome'] } },
    ...(realStack
      ? [
          {
            name: 'real-stack',
            testDir: './e2e/real',
            // Serial and without retries: every test creates real cases and runs, and a retry
            // would hide a flaky pipeline instead of reporting it.
            fullyParallel: false,
            retries: 0,
            use: { ...devices['Desktop Chrome'] },
          },
        ]
      : []),
  ],
  webServer: process.env.PLAYWRIGHT_BASE_URL
    ? undefined
    : {
        command: 'npm run preview -- --port 4173 --strictPort',
        url: baseURL,
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      },
});
