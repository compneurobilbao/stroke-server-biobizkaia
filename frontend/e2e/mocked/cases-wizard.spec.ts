import { expect, test, type Page } from '@playwright/test';
import { installMockApi, type MockHandle } from './mockApi';

const gz = (name: string) => ({ name, mimeType: 'application/gzip', buffer: Buffer.from('synthetic') });

function bodyOf(handle: MockHandle, method: string, path: string): unknown {
  const found = handle.requests.find((r) => r.method === method && r.path === path);
  expect(found, `${method} ${path} was sent`).toBeDefined();
  return JSON.parse(found?.body ?? 'null');
}

async function openWizard(page: Page) {
  await page.goto('/cases/new');
  await expect(page.getByRole('heading', { name: 'New case' })).toBeVisible();
}

test.describe('case creation wizard (mocked API)', () => {
  test('lesion mask already in MNI: create, upload, start and follow the run', async ({ page }) => {
    const handle = await installMockApi(page);
    await openWizard(page);

    await page.getByRole('radio', { name: /Lesion mask already in MNI/ }).check();
    await page.getByLabel('Sex (optional)').selectOption('male');
    await page.getByLabel('Age band (optional)').selectOption('70-74');
    await page.getByRole('button', { name: 'Next' }).click();

    await expect(page.getByLabel('CT scan (.nii.gz)')).toHaveCount(0);
    await page.getByLabel('Lesion mask in MNI space (.nii.gz)').setInputFiles(gz('whatever-name.nii.gz'));
    await page.getByRole('button', { name: 'Next' }).click();

    // Deployment default preselected, unavailable and unused slots marked.
    await expect(page.getByRole('combobox', { name: 'Functional disconnection' })).toHaveValue('fdc-stub');
    await expect(page.locator('[data-slot="cognitive-profile"]')).toContainText('Unavailable');
    await expect(page.locator('[data-slot="normalisation"]')).toContainText('Not used in this mode');
    await page.getByRole('combobox', { name: 'Functional disconnection' }).selectOption('fdc-alt');
    await page.getByRole('button', { name: 'Create case and start run' }).click();

    await expect(page).toHaveURL(/\/runs\/r1$/);
    await expect(page.getByRole('heading', { name: 'Run', exact: true })).toBeVisible();
    const timeline = page.getByRole('list', { name: 'Step timeline' });
    await expect(timeline.locator('[data-slot="functional-disconnection"]')).toContainText('fdc-alt');
    await expect(timeline.locator('[data-slot="normalisation"]')).toHaveAttribute('data-state', 'skipped');
    // The run page polls; the mock reports success from the second read.
    await expect(page.getByRole('button', { name: 'Download PDF report' })).toBeVisible({ timeout: 15_000 });
    await expect(timeline.locator('[data-slot="functional-disconnection"]')).toHaveAttribute('data-state', 'succeeded');
    await expect(page.getByRole('button', { name: 'Cancel run' })).toHaveCount(0);

    expect(bodyOf(handle, 'POST', '/v1/cases')).toEqual({ sex: 'male', age_band: '70-74' });
    expect(handle.requests.filter((r) => r.method === 'PUT').map((r) => r.path)).toEqual(['/v1/cases/c1/inputs/lesion_mni']);
    expect(bodyOf(handle, 'POST', '/v1/cases/c1/runs')).toEqual({
      pipeline_id: 'stroke-lnm',
      pipeline_version: '0.1.0',
      input_roles: ['lesion_mni'],
      implementations: { 'functional-disconnection': 'fdc-alt' },
    });
  });

  test('CT and lesion mask: uploads both inputs and keeps the default implementations', async ({ page }) => {
    const handle = await installMockApi(page);
    await openWizard(page);
    await page.getByRole('button', { name: 'Next' }).click();

    await page.getByLabel('CT scan (.nii.gz)').setInputFiles(gz('ct.nii.gz'));
    await expect(page.getByRole('button', { name: 'Next' })).toBeDisabled();
    await page.getByLabel('Lesion mask on the CT (.nii.gz)').setInputFiles(gz('mask.nii.gz'));
    await page.getByRole('button', { name: 'Next' }).click();

    await expect(page.getByRole('combobox', { name: 'CT lesion to MNI152' })).toHaveValue('ct-lesion-to-mni152');
    await page.getByRole('button', { name: 'Create case and start run' }).click();

    await expect(page).toHaveURL(/\/runs\/r1$/);
    const timeline = page.getByRole('list', { name: 'Step timeline' });
    await expect(timeline.locator('[data-slot="normalisation"]')).toContainText('ct-lesion-to-mni152');
    await expect(timeline.locator('[data-slot="cognitive-profile"]')).toHaveAttribute('data-state', 'unavailable');

    await timeline.locator('[data-slot="lesion-intake"]').getByRole('button', { name: 'Show log' }).click();
    await expect(page.getByText('lesion-qc: mask checked')).toBeVisible();

    expect(bodyOf(handle, 'POST', '/v1/cases')).toEqual({ sex: null, age_band: null });
    expect(handle.requests.filter((r) => r.method === 'PUT').map((r) => r.path)).toEqual([
      '/v1/cases/c1/inputs/ct',
      '/v1/cases/c1/inputs/lesion_native',
    ]);
    expect(bodyOf(handle, 'POST', '/v1/cases/c1/runs')).toMatchObject({
      input_roles: ['ct', 'lesion_native'],
      implementations: {},
    });
  });
});
