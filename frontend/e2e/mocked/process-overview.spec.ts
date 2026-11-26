import { expect, test } from './fixtures';
import { installMockApi } from './mockApi';

test('the home page explains the process before login and the page stays reachable after it', async ({ page }) => {
  await page.route('**/api/auth/me', (route) => route.fulfill({ status: 401, json: { detail: 'Not authenticated' } }));
  await page.goto('/login');
  const overview = page.getByRole('region', { name: 'How a lesion becomes a research report' });
  await expect(overview).toBeVisible();
  await expect(overview.getByText('flnm-fast, against a normative functional connectome.')).toBeVisible();
  await expect(overview.getByText('In development', { exact: true })).toHaveCount(5);
  await expect(page.getByText('Alpha', { exact: true })).toBeVisible();
});

test('signed-in users reach the process page from the navigation', async ({ page }) => {
  await installMockApi(page);
  await page.goto('/cases');
  await page.getByRole('link', { name: 'About the process' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'About the process' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'How a lesion becomes a research report' })).toBeVisible();
});
