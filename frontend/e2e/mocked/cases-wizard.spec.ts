import { expect, test, type Page } from '@playwright/test';
import { installMockApi, type MockHandle } from './mockApi';

const gz = (name: string) => ({ name, mimeType: 'application/gzip', buffer: Buffer.from('synthetic') });

function bodyOf(handle: MockHandle, method: string, path: string): unknown {
  const found = handle.requests.find((r) => r.method === method && r.path === path);
  expect(found, `${method} ${path} was sent`).toBeDefined();
  return JSON.parse(found?.body ?? 'null');
}

const PLAN_PATH = '/v1/pipelines/stroke-lnm/versions/0.1.0/plan';

function lastBody(handle: MockHandle, method: string, path: string): Record<string, unknown> {
  const sent = handle.requests.filter((r) => r.method === method && r.path === path);
  expect(sent.length, `${method} ${path} was sent`).toBeGreaterThan(0);
  return JSON.parse(sent[sent.length - 1].body ?? 'null') as Record<string, unknown>;
}

async function openWizard(page: Page) {
  await page.goto('/cases/new');
  await expect(page.getByRole('heading', { name: 'New case' })).toBeVisible();
}

/** Mode "lesion mask already in MNI" through the first two steps; the analysis step is then shown. */
async function uploadMniMask(page: Page) {
  await page.getByRole('radio', { name: /Lesion mask already in MNI/ }).check();
  await page.getByRole('button', { name: 'Next' }).click();
  await page.getByLabel('Lesion mask in MNI space (.nii.gz)').setInputFiles(gz('whatever-name.nii.gz'));
  await page.getByRole('button', { name: 'Next' }).click();
  await expect(page.getByRole('group', { name: 'Analysis' })).toBeVisible();
}

const planRow = (page: Page, slot: string) => page.locator(`.plan-slot[data-slot="${slot}"]`);

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

    // Without presets the full plan is offered; the preview marks slots that do not apply.
    await expect(page.getByRole('radio', { name: /Full plan/ })).toBeChecked();
    await expect(planRow(page, 'normalisation')).toHaveAttribute('data-plan-state', 'condition_false');
    await expect(planRow(page, 'lesion-intake')).toHaveAttribute('data-plan-state', 'target');
    await page.getByRole('button', { name: 'Next' }).click();

    // Deployment default preselected, unavailable slots marked, slots outside the plan not listed.
    await expect(page.getByRole('combobox', { name: 'Functional disconnection' })).toHaveValue('fdc-stub');
    await expect(page.locator('[data-slot="cognitive-profile"]')).toContainText('Unavailable');
    await expect(page.locator('[data-slot="normalisation"]')).toHaveCount(0);
    await page.getByRole('combobox', { name: 'Functional disconnection' }).selectOption('fdc-alt');
    await page.getByRole('button', { name: 'Create case and start run' }).click();

    await expect(page).toHaveURL(/\/runs\/r1$/);
    await expect(page.getByRole('heading', { name: 'Run', exact: true })).toBeVisible();
    await expect(page.getByTestId('run-plan')).toContainText('Full plan');
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
    await expect(planRow(page, 'normalisation')).toHaveAttribute('data-plan-state', 'target');
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

test.describe('analysis step (mocked API)', () => {
  test('preset flow: the preview shows the plan, the run request names the preset, the run page shows it', async ({ page }) => {
    const handle = await installMockApi(page, { presets: true });
    await openWizard(page);
    await uploadMniMask(page);

    // The default preset is preselected; the CT-only preset is not offered in this mode; no custom option.
    await expect(page.getByRole('radio', { name: /Full analysis/ })).toBeChecked();
    await expect(page.getByRole('radio', { name: /CT normalisation only/ })).toHaveCount(0);
    await expect(page.getByRole('radio', { name: /Custom/ })).toHaveCount(0);

    await page.getByRole('radio', { name: /Disconnection analysis of an MNI lesion mask/ }).check();
    await expect(planRow(page, 'functional-disconnection')).toHaveAttribute('data-plan-state', 'target');
    await expect(planRow(page, 'lesion-intake')).toHaveAttribute('data-plan-state', 'upstream');
    await expect(planRow(page, 'lesion-intake')).toContainText('Added: needed by Functional disconnection');
    await expect(planRow(page, 'cognitive-profile')).toHaveAttribute('data-plan-state', 'not_requested');
    await expect(planRow(page, 'normalisation')).toHaveAttribute('data-plan-state', 'condition_false');
    await expect(page.getByTestId('plan-summary')).toContainText('can run');
    expect(lastBody(handle, 'POST', PLAN_PATH)).toMatchObject({
      input_roles: ['lesion_mni'],
      preset: { id: 'mni-mask-analysis', version: '1.0.0' },
    });

    await page.getByRole('button', { name: 'Next' }).click();
    // Only the planned slots get a selector.
    await expect(page.getByRole('combobox', { name: 'Functional disconnection' })).toHaveValue('fdc-stub');
    await expect(page.locator('[data-slot="cognitive-profile"]')).toHaveCount(0);
    await expect(page.locator('[data-slot="lesion-intake"]')).toContainText('Added by the plan');
    await page.getByRole('button', { name: 'Create case and start run' }).click();

    await expect(page).toHaveURL(/\/runs\/r1$/);
    const created = bodyOf(handle, 'POST', '/v1/cases/c1/runs') as Record<string, unknown>;
    expect(created).toMatchObject({ preset: { id: 'mni-mask-analysis', version: '1.0.0' }, implementations: {} });
    expect(created).not.toHaveProperty('targets');

    await expect(page.getByTestId('run-plan')).toContainText('Preset mni-mask-analysis 1.0.0');
    const timeline = page.getByRole('list', { name: 'Step timeline' });
    await expect(timeline.locator('[data-slot="lesion-intake"]')).toContainText('Added by the plan');
    // The slot outside the plan is collapsed with a count; the one that does not apply stays in the list.
    await expect(page.getByTestId('not-requested')).toContainText('Not part of this plan (1)');
    await expect(page.getByTestId('not-requested').locator('details')).not.toHaveAttribute('open', '');
    await expect(page.getByTestId('not-requested').locator('[data-slot="cognitive-profile"]')).toHaveAttribute(
      'data-state',
      'skipped',
    );
    await expect(timeline.locator('[data-slot="normalisation"]')).toHaveAttribute('data-state', 'skipped');
    await expect(page.getByRole('button', { name: 'Download PDF report' })).toBeVisible({ timeout: 15_000 });
  });

  test('custom flow: free mode allowed, outputs checklist, upstream slot added, refusal shown, targets sent', async ({ page }) => {
    const handle = await installMockApi(page, { presets: true, freeMode: true });
    await openWizard(page);
    await uploadMniMask(page);

    await page.getByRole('radio', { name: /Custom/ }).check();
    await expect(page.getByText('Choose at least one output to see the plan.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Next' })).toBeDisabled();

    // A required output without an enabled implementation is refused, with the reason in plain language.
    await page.getByRole('combobox', { name: 'Cognitive profile: request' }).selectOption('required');
    await expect(
      page.getByRole('alert').filter({ hasText: 'Cognitive profile has no enabled implementation in this deployment.' }),
    ).toBeVisible();
    await expect(page.getByTestId('plan-summary')).toContainText('cannot run');
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('button', { name: 'Create case and start run' })).toBeDisabled();
    await page.getByRole('button', { name: 'Back' }).click();
    await page.getByRole('combobox', { name: 'Cognitive profile: request' }).selectOption('');

    // Functional disconnection pulls in the lesion intake as an upstream slot.
    await page.getByRole('combobox', { name: 'Functional disconnection: request' }).selectOption('required');
    await expect(planRow(page, 'functional-disconnection')).toHaveAttribute('data-plan-state', 'target');
    await expect(planRow(page, 'lesion-intake')).toHaveAttribute('data-plan-state', 'upstream');
    await expect(page.getByTestId('plan-summary')).toContainText('can run');
    // The same output as best effort does not refuse the plan.
    await page.getByRole('combobox', { name: 'Cognitive profile: request' }).selectOption('if_available');
    await expect(planRow(page, 'cognitive-profile')).toHaveAttribute('data-plan-state', 'unavailable');
    await expect(page.getByTestId('plan-summary')).toContainText('can run');
    expect(lastBody(handle, 'POST', PLAN_PATH)).toMatchObject({
      targets: { required: ['functional-disconnection'], if_available: ['cognitive-profile'] },
    });

    await page.getByRole('button', { name: 'Next' }).click();
    await page.getByRole('button', { name: 'Create case and start run' }).click();
    await expect(page).toHaveURL(/\/runs\/r1$/);
    const created = bodyOf(handle, 'POST', '/v1/cases/c1/runs') as Record<string, unknown>;
    expect(created.targets).toEqual({ required: ['functional-disconnection'], if_available: ['cognitive-profile'] });
    expect(created).not.toHaveProperty('preset');

    const plan = page.getByTestId('run-plan');
    await expect(plan).toContainText('Custom plan');
    await expect(plan).toContainText('functional-disconnection.fdisconnection_map');
    await expect(plan).toContainText('cognitive-profile.profile');
    await expect(page.getByTestId('not-requested')).toHaveCount(0);
  });

  test('custom is not offered when the deployment does not allow free mode', async ({ page }) => {
    await installMockApi(page, { presets: true, freeMode: false });
    await openWizard(page);
    await uploadMniMask(page);
    await expect(page.getByRole('radio', { name: /Full analysis/ })).toBeVisible();
    await expect(page.getByRole('radio', { name: /Custom/ })).toHaveCount(0);
  });

  test('a refusal at creation that the preview did not predict is shown and can be retried', async ({ page }) => {
    const handle = await installMockApi(page, { refuseCreateOnce: true });
    await openWizard(page);
    await uploadMniMask(page);
    await page.getByRole('button', { name: 'Next' }).click();
    await page.getByRole('button', { name: 'Create case and start run' }).click();

    const refusal = page.getByTestId('create-error');
    await expect(refusal).toContainText('The server refused to start this run:');
    await expect(refusal).toContainText(
      'The installed version of the step for Functional disconnection cannot read the run record of this plan.',
    );
    await expect(page).toHaveURL(/\/cases\/new$/);
    // The case already exists: the retry reuses it.
    await page.getByRole('button', { name: 'Create case and start run' }).click();
    await expect(page).toHaveURL(/\/runs\/r1$/);
    expect(handle.requests.filter((r) => r.method === 'POST' && r.path === '/v1/cases')).toHaveLength(1);
  });
});
