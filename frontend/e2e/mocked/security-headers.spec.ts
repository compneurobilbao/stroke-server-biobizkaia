import { proxySecurityHeaders } from '../securityHeaders';
import { expect, test } from './fixtures';

test('the built app is served with the security headers of the reverse proxy', async ({ page }) => {
  const expected = proxySecurityHeaders();
  expect(Object.keys(expected).sort()).toEqual(
    ['Content-Security-Policy', 'Referrer-Policy', 'X-Content-Type-Options', 'X-Frame-Options'].sort(),
  );
  await page.route('**/api/**', (route) => route.fulfill({ status: 401, json: { detail: 'Not authenticated' } }));

  const response = await page.goto('/');

  const headers = response?.headers() ?? {};
  for (const [name, value] of Object.entries(expected)) {
    expect(headers[name.toLowerCase()], name).toBe(value);
  }
  // The app renders under the policy (violations fail the test through the fixture).
  await expect(page.locator('#root')).not.toBeEmpty();
});
