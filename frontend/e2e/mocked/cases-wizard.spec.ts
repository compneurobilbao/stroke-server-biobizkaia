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

test.describe('declared origin of a supplied mask (mocked API)', () => {
  test('an external tool with its version is sent with the upload and shown on the run page', async ({ page }) => {
    const handle = await installMockApi(page);
    await openWizard(page);
    await page.getByRole('radio', { name: /Lesion mask already in MNI/ }).check();
    await page.getByRole('button', { name: 'Next' }).click();

    await expect(page.getByText(/shown on the run page and in the report as declared by the user/)).toBeVisible();
    await page.getByLabel('Lesion mask in MNI space (.nii.gz)').setInputFiles(gz('whatever-name.nii.gz'));
    await page.getByLabel('Origin of the mask').selectOption('external-tool');
    await expect(page.getByRole('button', { name: 'Next' })).toBeDisabled();
    // Free text is refused in the browser: spaces are not allowed in a tool name.
    await page.getByLabel('Tool name').fill('drawn by someone');
    await page.getByLabel('Tool version').fill('1.4.2');
    await expect(page.getByRole('alert')).toContainText('Use only letters, digits');
    await expect(page.getByRole('button', { name: 'Next' })).toBeDisabled();
    await page.getByLabel('Tool name').fill('example-tool');
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('group', { name: 'Analysis' })).toBeVisible();
    await page.getByRole('button', { name: 'Next' }).click();
    await page.getByRole('button', { name: 'Create case and start run' }).click();

    await expect(page).toHaveURL(/\/runs\/r1$/);
    await expect(page.getByTestId('origin-lesion_mni')).toContainText('Declared by the user');
    await expect(page.getByTestId('origin-lesion_mni')).toContainText('external tool example-tool 1.4.2');
    const put = handle.requests.find((r) => r.method === 'PUT' && r.path === '/v1/cases/c1/inputs/lesion_mni');
    expect(put?.contentType).toContain('multipart/form-data');
    expect(put?.body).toContain('name="origin"');
    expect(put?.body).toContain('{"kind":"external-tool","tool":"example-tool","version":"1.4.2"}');
  });

  test('without a declaration no origin is sent and the run page shows none', async ({ page }) => {
    const handle = await installMockApi(page);
    await openWizard(page);
    await uploadMniMask(page);
    await page.getByRole('button', { name: 'Next' }).click();
    await page.getByRole('button', { name: 'Create case and start run' }).click();
    await expect(page).toHaveURL(/\/runs\/r1$/);
    await expect(page.getByTestId('origin-lesion_mni')).toHaveCount(0);
    const put = handle.requests.find((r) => r.method === 'PUT');
    expect(put?.body).not.toContain('name="origin"');
  });
});

test.describe('example lesion (mocked API)', () => {
  test('no button when the deployment offers no example', async ({ page }) => {
    await installMockApi(page);
    await openWizard(page);
    await page.getByRole('radio', { name: /Lesion mask already in MNI/ }).check();
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByLabel('Lesion mask in MNI space (.nii.gz)')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Use the example lesion (synthetic)' })).toHaveCount(0);
  });

  test('the example is attached by the server and shown as a synthetic example on the run page', async ({ page }) => {
    const handle = await installMockApi(page, { exampleInputs: true });
    await openWizard(page);
    await page.getByRole('radio', { name: /Lesion mask already in MNI/ }).check();
    await page.getByRole('button', { name: 'Next' }).click();
    await page.getByRole('button', { name: 'Use the example lesion (synthetic)' }).click();
    await expect(page.getByText(/Synthetic example selected/)).toBeVisible();
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('group', { name: 'Analysis' })).toBeVisible();
    await page.getByRole('button', { name: 'Next' }).click();
    await page.getByRole('button', { name: 'Create case and start run' }).click();

    await expect(page).toHaveURL(/\/runs\/r1$/);
    await expect(page.getByTestId('origin-lesion_mni')).toContainText('Provided by the platform: Synthetic example lesion');
    expect(handle.requests.filter((r) => r.method === 'PUT')).toHaveLength(0);
    const post = handle.requests.find((r) => r.method === 'POST' && r.path === '/v1/cases/c1/inputs/lesion_mni/example');
    expect(JSON.parse(post?.body ?? 'null')).toEqual({ example_id: 'mni-lesion-left-medium' });
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

test.describe('wizard and run page follow-ups (mocked API)', () => {
  test('the plan preview says which optional input will be missing, and names the external tool and level of a candidate', async ({ page }) => {
    await installMockApi(page, { presets: true });
    await openWizard(page);
    await uploadMniMask(page);

    // The full analysis plans every step that applies: nothing is missing.
    await expect(planRow(page, 'functional-disconnection')).toBeVisible();
    await expect(page.getByTestId('plan-missing-inputs')).toHaveCount(0);

    // The preset leaves the cognitive profile out, which the functional disconnection can optionally use.
    await page.getByRole('radio', { name: /Disconnection analysis of an MNI lesion mask/ }).check();
    await expect(page.getByTestId('plan-missing-inputs')).toContainText(
      'Functional disconnection will run without the optional input "cognet profile": Cognitive profile will not run in this plan.',
    );
    // The mask the user supplied feeds the other optional port: only one note.
    await expect(page.getByTestId('plan-missing-inputs').locator('li')).toHaveCount(1);

    await page.getByRole('button', { name: 'Next' }).click();
    const row = page.locator('[data-slot="functional-disconnection"]');
    const candidates = row.getByRole('list', { name: 'Candidate implementations' });
    await expect(candidates.locator('[data-impl="fdc-alt"]')).toContainText('External tool: ext-tool 2.3.4 (MIT)');
    await expect(candidates.locator('[data-impl="fdc-alt"]')).toContainText('reference-tested - experimental');
    await expect(candidates.locator('[data-impl="fdc-stub"]')).toContainText('unvalidated');
    await expect(candidates.locator('[data-impl="fdc-stub"]')).not.toContainText('External tool');
    await expect(candidates.locator('[data-impl="fdc-stub"]')).toHaveAttribute('data-current', 'true');
    await row.getByRole('combobox').selectOption('fdc-alt');
    await expect(candidates.locator('[data-impl="fdc-alt"]')).toHaveAttribute('data-current', 'true');
  });

  test('stages in development are greyed and not selectable, and the CT mode is disabled', async ({ page }) => {
    await installMockApi(page, { inDevelopment: true });
    await openWizard(page);
    // Normalisation has no enabled implementation: CT mode cannot be used, the wizard starts in MNI mode.
    await expect(page.getByRole('radio', { name: /CT and lesion mask/ })).toBeDisabled();
    await expect(page.getByRole('radio', { name: /Lesion mask already in MNI/ })).toBeChecked();
    await uploadMniMask(page);

    const development = page.getByTestId('in-development');
    await expect(development.locator('[data-development]')).toHaveCount(4);
    await expect(development.locator('.tag', { hasText: 'In development' })).toHaveCount(4);
    await expect(development.getByText('Lesion segmentation')).toBeVisible();
    await expect(development.getByText('Lesion bank')).toBeVisible();
    for (const control of ['radio', 'checkbox', 'combobox', 'button']) {
      await expect(development.getByRole(control as 'radio')).toHaveCount(0);
    }
    // The plan marks the slot in development instead of "does not apply".
    await expect(planRow(page, 'normalisation')).toContainText('In development');

    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByTestId('in-development').locator('[data-development]')).toHaveCount(4);
    await expect(page.locator('[data-slot="normalisation"]')).toHaveCount(0);
  });

  test('nothing is listed as in development for a deployment that has everything mounted', async ({ page }) => {
    await installMockApi(page);
    await openWizard(page);
    await expect(page.getByRole('radio', { name: /CT and lesion mask/ })).toBeEnabled();
    await uploadMniMask(page);
    await expect(page.getByTestId('in-development')).toHaveCount(0);
  });

  test('the run page lists the stages in development as not part of the run', async ({ page }) => {
    await installMockApi(page, { inDevelopment: true, seedCase: { run: 'succeeded' } });
    await page.goto('/runs/r1');
    const development = page.getByTestId('in-development');
    await expect(development.getByRole('heading', { name: 'Not part of this run' })).toBeVisible();
    await expect(development.locator('[data-development]')).toHaveCount(4);
  });

  test('run again on the same case: no files step, nothing is uploaded again, the run goes to the same case', async ({ page }) => {
    const handle = await installMockApi(page, { seedCase: { run: 'succeeded', origin: { kind: 'manual' } } });
    await page.goto('/runs/r1');
    await page.getByRole('button', { name: 'Run again with the same plan' }).click();

    await expect(page.getByRole('heading', { name: 'New run on this case' })).toBeVisible();
    await expect(page.getByTestId('same-case-note')).toBeVisible();
    await expect(page.getByRole('group', { name: 'Analysis' })).toBeVisible();
    await expect(page.getByLabel('Lesion mask in MNI space (.nii.gz)')).toHaveCount(0);
    await page.getByRole('button', { name: 'Next' }).click();
    await page.getByRole('button', { name: 'Create case and start run' }).click();

    await expect(page).toHaveURL(/\/runs\/r1$/);
    expect(handle.requests.filter((r) => r.method === 'POST' && r.path === '/v1/cases')).toHaveLength(0);
    expect(handle.requests.filter((r) => r.method === 'PUT')).toHaveLength(0);
    expect(bodyOf(handle, 'POST', '/v1/cases/c1/runs')).toMatchObject({ input_roles: ['lesion_mni'] });
  });

  test('run again on the same case is not possible while the run is active', async ({ page }) => {
    await installMockApi(page, { seedCase: { run: 'running' } });
    await page.goto('/runs/r1');
    await expect(page.getByRole('button', { name: 'Run again with the same plan' })).toBeDisabled();
    await expect(page.getByText(/A run of this case is active/)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Run again with new files' })).toBeEnabled();
  });

  test('the case list shows the declared origin; the owner edits it before the first run', async ({ page }) => {
    const handle = await installMockApi(page, { seedCase: { origin: { kind: 'manual' } } });
    await page.goto('/cases');
    await expect(page.getByTestId('origin-c1')).toContainText('drawn by hand');

    await page.getByRole('link', { name: 'sub-0001 / ses-01' }).click();
    await expect(page.getByTestId('origin-lesion_mni')).toContainText('Declared by the user (not verified): drawn by hand');
    await page.getByRole('button', { name: 'Edit origin' }).click();
    await page.getByLabel('Origin of the mask').selectOption('external-tool');
    await page.getByLabel('Tool name').fill('example-tool');
    await page.getByLabel('Tool version').fill('1.4.2');
    await page.getByRole('button', { name: 'Save origin' }).click();
    await expect(page.getByTestId('origin-lesion_mni')).toContainText('external tool example-tool 1.4.2');
    expect(bodyOf(handle, 'PATCH', '/v1/cases/c1/inputs/lesion_mni')).toEqual({
      origin: { kind: 'external-tool', tool: 'example-tool', version: '1.4.2' },
    });

    await page.getByRole('link', { name: 'Back to cases' }).click();
    await expect(page.getByTestId('origin-c1')).toContainText('external tool example-tool 1.4.2');
  });

  test('after the first run the origin cannot be edited', async ({ page }) => {
    await installMockApi(page, { seedCase: { run: 'succeeded', origin: { kind: 'manual' } } });
    await page.goto('/cases/c1');
    await expect(page.getByTestId('origin-lesion_mni')).toContainText('drawn by hand');
    await expect(page.getByRole('button', { name: 'Edit origin' })).toHaveCount(0);
    await expect(page.getByTestId('origin-locked')).toBeVisible();
  });
});
