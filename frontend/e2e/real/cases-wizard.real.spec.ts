/**
 * Case wizard against the real Compose stack (card F0-31a).
 *
 * Nothing is mocked: the browser talks to the reverse proxy of the running stack (API, the
 * `pipelines` profile with the docker executor, step images built by CI, the `ci` implementation
 * profile). The inputs are the synthetic NIfTI files committed under `fixtures/data/`.
 *
 * - MNI-mask mode runs the real steps (lesion QC, tract disconnection, COGNET-STROKE, report)
 *   and the functional-disconnection stub to a finished run.
 * - CT mode runs the real normalisation step (ct-lesion-to-mni152) on a reference case that the
 *   workflow builds from a public head CT template (pipelines/stroke-lnm/ci/ct_reference.sh;
 *   downloaded in CI only, never committed) and then the same steps as above to a finished run.
 *   The synthetic CT under `fixtures/data/` is too coarse for that step (it is rejected), so it
 *   is not used here.
 *
 * Environment: PLAYWRIGHT_BASE_URL (config), E2E_USER_EMAIL, E2E_USER_PASSWORD (a researcher
 * created by scripts/e2e/real_stack.sh; never committed), E2E_CT_REFERENCE_DIR (directory with
 * ct.nii.gz and lesion_mask_native.nii.gz of the CT reference case).
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page } from '@playwright/test';

const fixture = (...parts: string[]) =>
  fileURLToPath(new URL(['..', '..', '..', 'fixtures', 'data', ...parts].join('/'), import.meta.url));

const MNI_MASK = fixture('mni_masks', 'lesion_left_medium.nii.gz');

function ctReference(): { ct: string; mask: string } {
  const dir = process.env.E2E_CT_REFERENCE_DIR;
  if (!dir) throw new Error('E2E_CT_REFERENCE_DIR must be set');
  return { ct: `${dir}/ct.nii.gz`, mask: `${dir}/lesion_mask_native.nii.gz` };
}

/** Upper bound for a run of the real steps; the test fails earlier as soon as the run fails. */
const RUN_TIMEOUT_MS = 12 * 60_000;
const TERMINAL = ['succeeded', 'failed', 'canceled'];

const RUN_STATE = 'section[aria-labelledby="run-title"] .section-title .run-badge';

function credentials(): { email: string; password: string } {
  const email = process.env.E2E_USER_EMAIL;
  const password = process.env.E2E_USER_PASSWORD;
  if (!email || !password) throw new Error('E2E_USER_EMAIL and E2E_USER_PASSWORD must be set');
  return { email, password };
}

async function signIn(page: Page) {
  const { email, password } = credentials();
  await page.goto('/login');
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
  await expect(page).not.toHaveURL(/\/login/);
}

async function openWizard(page: Page) {
  await page.goto('/cases/new');
  await expect(page.getByRole('heading', { name: 'New case' })).toBeVisible();
}

/** Mode "lesion mask already in MNI", through the first two wizard steps. */
async function uploadMniMask(page: Page) {
  await page.getByRole('radio', { name: /Lesion mask already in MNI/ }).check();
  await page.getByLabel('Sex (optional)').selectOption('female');
  await page.getByLabel('Age band (optional)').selectOption('60-64');
  await page.getByRole('button', { name: 'Next' }).click();
  await expect(page.getByLabel('CT scan (.nii.gz)')).toHaveCount(0);
  await page.getByLabel('Lesion mask in MNI space (.nii.gz)').setInputFiles(MNI_MASK);
  await page.getByRole('button', { name: 'Next' }).click();
}

/** Default implementation of every slot that applies in MNI-mask mode with the `ci` profile. */
async function expectCiDefaults(page: Page) {
  const choice = (title: string) => page.getByRole('combobox', { name: title, exact: true });
  await expect(choice('Lesion intake and QC')).toHaveValue('lesion-qc');
  await expect(choice('Functional disconnection')).toHaveValue('fdisconnection-stub');
  await expect(choice('Tract-wise structural disconnection')).toHaveValue('lnm-tracts');
  await expect(choice('COGNET-STROKE cognitive profile')).toHaveValue('cognet-stroke');
  await expect(choice('Research report')).toHaveValue('research-report');
  await expect(page.locator('[data-slot="normalisation"]')).toContainText('Not used in this mode');
}

/** Clicks Start and returns the body of the run-creation request (the page then shows the run). */
async function startRun(page: Page) {
  const bodies: Record<string, unknown>[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST' && /\/api\/v1\/cases\/[^/]+\/runs$/.test(r.url())) {
      bodies.push(r.postDataJSON() as Record<string, unknown>);
    }
  });
  await page.getByRole('button', { name: 'Create case and start run' }).click();
  // Creating the case, uploading and creating the run is quick; fail early with what the page says.
  await expect(page, 'the wizard moves to the run page').toHaveURL(/\/runs\/[0-9a-f-]{36}$/, { timeout: 60_000 });
  expect(bodies).toHaveLength(1);
  return bodies[0];
}

/** Waits for a terminal run state (failing early with the server's message) and checks it succeeded. */
async function waitForRunSuccess(page: Page) {
  await expect(page.getByRole('heading', { name: 'Run', exact: true })).toBeVisible();
  await expect
    .poll(async () => (await page.locator(RUN_STATE).getAttribute('data-state')) ?? '', {
      timeout: RUN_TIMEOUT_MS,
      intervals: [3_000],
      message: 'the run reaches a terminal state',
    })
    .toMatch(new RegExp(`^(${TERMINAL.join('|')})$`));
  const state = await page.locator(RUN_STATE).getAttribute('data-state');
  const details = (await page.locator('section[aria-labelledby="run-title"] dd').allInnerTexts()).join(' | ');
  expect(state, `run ended as ${state}; details: ${details}`).toBe('succeeded');
}

const timeline = (page: Page) => page.getByRole('list', { name: 'Step timeline' });

async function expectStepsSucceeded(page: Page, mode: 'mni' | 'ct' = 'mni') {
  const slot = (name: string) => timeline(page).locator(`[data-slot="${name}"]`);
  if (mode === 'ct') {
    await expect(slot('normalisation')).toHaveAttribute('data-state', 'succeeded');
    await expect(slot('normalisation')).toContainText('ct-lesion-to-mni152');
    await expect(slot('normalisation')).not.toContainText('(stub)');
  } else {
    await expect(slot('normalisation')).toHaveAttribute('data-state', 'skipped');
  }
  for (const name of ['lesion-intake', 'functional-disconnection', 'tract-disconnection', 'cognet-profile', 'report']) {
    await expect(slot(name), `slot ${name}`).toHaveAttribute('data-state', 'succeeded');
  }
  await expect(slot('lesion-intake')).toContainText('lesion-qc');
  await expect(slot('functional-disconnection')).toContainText('fdisconnection-stub');
  await expect(slot('functional-disconnection')).toContainText('(stub)');
  await expect(slot('tract-disconnection')).toContainText('lnm-tracts');
  await expect(slot('cognet-profile')).toContainText('cognet-stroke');
  await expect(page.getByRole('button', { name: 'Cancel run' })).toHaveCount(0);
}

async function expectOutputs(page: Page, mode: 'mni' | 'ct' = 'mni') {
  const artefacts = page.locator('section', { has: page.getByRole('heading', { name: 'Artefacts' }) });
  if (mode === 'ct') {
    await expect(artefacts.getByRole('row', { name: /ct_mni/ }).first()).toBeVisible();
    await expect(artefacts.getByRole('row', { name: /lesion_mask_mni/ }).first()).toBeVisible();
  }
  await expect(artefacts.getByRole('row', { name: /tract_disconnection/ })).toBeVisible();
  await expect(artefacts.getByRole('row', { name: /fdisconnection_map/ })).toContainText('synthetic');
  await expect(artefacts.getByRole('row', { name: /report_pdf/ })).toBeVisible();
}

async function expectPdfDownload(page: Page) {
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Download PDF report' }).first().click(),
  ]);
  const bytes = readFileSync(await download.path());
  expect(bytes.length).toBeGreaterThan(1_000);
  expect(bytes.subarray(0, 5).toString('latin1')).toBe('%PDF-');
}

/**
 * Opens the read-only viewer on the finished run (card F0-32): the lesion mask and the
 * functional-disconnection stub map are downloaded with the session token and drawn by NiiVue
 * in the real browser. MNI-mask mode has no CT in MNI space, so there is no background and the
 * page says so. Requests to other origins would mean a CDN asset: none are allowed.
 */
async function expectViewer(page: Page, mode: 'mni' | 'ct' = 'mni') {
  const baseOrigin = new URL(page.url()).origin;
  const external: string[] = [];
  page.on('request', (request) => {
    const url = request.url();
    if (!/^(data|blob):/.test(url) && new URL(url).origin !== baseOrigin) external.push(url);
  });
  await page.getByRole('link', { name: 'View images' }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}\/view$/);
  const viewer = page.locator('.viewer-layout');
  await expect(viewer).toHaveAttribute('data-state', 'ready', { timeout: 60_000 });
  if (mode === 'ct') {
    // the registered CT of the normalisation step is the background
    await expect(viewer).toHaveAttribute('data-layers', 'template,lesion,disconnection');
    await expect(page.getByTestId('no-template')).toHaveCount(0);
  } else {
    await expect(viewer).toHaveAttribute('data-layers', 'lesion,disconnection');
    await expect(page.getByTestId('no-template')).toBeVisible();
  }
  await expect(page.getByRole('group', { name: /Disconnection map/ })).toContainText('synthetic');
  const box = await page.getByRole('img', { name: /Three-plane view/ }).boundingBox();
  expect(box?.width ?? 0).toBeGreaterThan(200);
  expect(external, 'requests to other origins').toEqual([]);
}

test.describe.configure({ mode: 'serial' });

test.describe('case creation wizard (real stack)', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test('lesion mask already in MNI: real steps and the stub run to success', async ({ page }) => {
    test.setTimeout(RUN_TIMEOUT_MS + 120_000);
    await openWizard(page);
    await uploadMniMask(page);
    await expectCiDefaults(page);

    const body = await startRun(page);
    // Only choices that differ from the deployment default are sent.
    expect(body).toMatchObject({ pipeline_id: 'stroke-lnm', input_roles: ['lesion_mni'], implementations: {} });

    await waitForRunSuccess(page);
    await expectStepsSucceeded(page);
    await expect(timeline(page).locator('[data-slot="report"]')).toContainText('deployment default');
    await expect(page.getByText('No report yet.')).toHaveCount(0);
    await expectOutputs(page);
    await expectPdfDownload(page);

    // The log of the real lesion-QC step is readable through the API.
    await timeline(page).locator('[data-slot="lesion-intake"]').getByRole('button', { name: 'Show log' }).click();
    await expect(page.getByLabel(/^Log of lesion-intake/)).toBeVisible();

    await expectViewer(page);
  });

  test('lesion mask already in MNI: a non-default report implementation is sent and run', async ({ page }) => {
    test.setTimeout(RUN_TIMEOUT_MS + 120_000);
    await openWizard(page);
    await uploadMniMask(page);
    await expectCiDefaults(page);
    await page.getByRole('combobox', { name: 'Research report', exact: true }).selectOption('research-report-es');

    const body = await startRun(page);
    expect(body).toMatchObject({
      pipeline_id: 'stroke-lnm',
      input_roles: ['lesion_mni'],
      implementations: { report: 'research-report-es' },
    });

    await waitForRunSuccess(page);
    await expectStepsSucceeded(page);
    const report = timeline(page).locator('[data-slot="report"]');
    await expect(report).toContainText('research-report-es');
    await expect(report).toContainText('chosen by you');
    await expectPdfDownload(page);
  });

  test('CT and lesion mask: the real normalisation step and the rest run to success', async ({ page }) => {
    test.setTimeout(RUN_TIMEOUT_MS + 120_000);
    const { ct, mask } = ctReference();
    await openWizard(page);
    // CT mode is the default; the age band is bound to the normalisation parameter
    await page.getByLabel('Age band (optional)').selectOption('60-64');
    await page.getByRole('button', { name: 'Next' }).click();
    await page.getByLabel('CT scan (.nii.gz)').setInputFiles(ct);
    await page.getByLabel('Lesion mask on the CT (.nii.gz)').setInputFiles(mask);
    await page.getByRole('button', { name: 'Next' }).click();

    const normalisation = page.locator('[data-slot="normalisation"]');
    await expect(normalisation).not.toHaveAttribute('data-state', 'unavailable');
    await expect(page.getByRole('combobox', { name: 'CT lesion to MNI152', exact: true })).toHaveValue(
      'ct-lesion-to-mni152',
    );
    await expect(page.getByRole('combobox', { name: 'Lesion intake and QC', exact: true })).toHaveValue('lesion-qc');
    await expect(page.getByRole('alert').filter({ hasText: 'required step is unavailable' })).toHaveCount(0);

    const body = await startRun(page);
    expect(body).toMatchObject({
      pipeline_id: 'stroke-lnm',
      input_roles: expect.arrayContaining(['ct', 'lesion_native']),
      implementations: {},
    });

    await waitForRunSuccess(page);
    await expectStepsSucceeded(page, 'ct');
    await expectOutputs(page, 'ct');
    await expectPdfDownload(page);
    await expectViewer(page, 'ct');
  });
});
