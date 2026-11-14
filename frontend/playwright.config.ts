import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end tests of the case wizard and run page (card F0-31).
 *
 * Mock mode (current): the tests intercept `/api` in the browser (e2e/mockApi.ts), so only the
 * built frontend is served. Run `npm run build` first; the web server below serves `dist/`.
 * Set PLAYWRIGHT_BASE_URL to test an already running frontend instead of starting `vite preview`.
 */
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
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: process.env.PLAYWRIGHT_BASE_URL
    ? undefined
    : {
        command: 'npm run preview -- --port 4173 --strictPort',
        url: baseURL,
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      },
});
