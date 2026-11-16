import { expect, test, type Page } from '@playwright/test';
import { installMockApi } from './mockApi';
import { syntheticDisconnection, syntheticLesion, syntheticTemplate } from './nifti';

const artefact = (id: string, type: string) => ({
  id,
  case_id: 'c1',
  pipeline_run_id: 'r1',
  origin: 'step',
  port: type,
  artefact_type: type,
  filename: 'stored-name.nii.gz',
  size_bytes: 1000,
  synthetic: true,
  created_at: '2026-11-14T10:00:00Z',
});

const FILES: Record<string, { type: string; bytes: () => Buffer }> = {
  'art-ct': { type: 'ct_mni', bytes: syntheticTemplate },
  'art-lesion': { type: 'lesion_mask_mni', bytes: syntheticLesion },
  'art-map': { type: 'fdisconnection_map', bytes: syntheticDisconnection },
};

interface Seen {
  downloads: { url: string; authorization: string | undefined }[];
  external: string[];
}

/** Serves the artefact list and downloads of run r1 from memory on top of the shared mocked API. */
async function serveArtefacts(page: Page, ids: string[]): Promise<Seen> {
  const seen: Seen = { downloads: [], external: [] };
  await installMockApi(page);
  await page.route('**/api/v1/runs/r1/artifacts', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(ids.map((id) => artefact(id, FILES[id].type))),
    }),
  );
  await page.route('**/api/v1/runs/r1/artifacts/*/download', (route) => {
    const request = route.request();
    const id = request.url().split('/artifacts/')[1].split('/')[0];
    seen.downloads.push({ url: request.url(), authorization: request.headers().authorization });
    return route.fulfill({ status: 200, contentType: 'application/octet-stream', body: FILES[id].bytes() });
  });
  // Offline requirement: nothing may be requested from another origin (no CDN assets).
  const origin = new URL(process.env.PLAYWRIGHT_BASE_URL ?? 'http://127.0.0.1:4173').origin;
  page.on('request', (request) => {
    const url = request.url();
    if (!url.startsWith('data:') && !url.startsWith('blob:') && new URL(url).origin !== origin) seen.external.push(url);
  });
  return seen;
}

const layout = (page: Page) => page.locator('.viewer-layout');
const canvas = (page: Page) => page.getByRole('img', { name: /Three-plane view/ });

test.describe('read-only viewer (mocked API)', () => {
  test('opens from the run page with all three layers and no external request', async ({ page }) => {
    const seen = await serveArtefacts(page, ['art-ct', 'art-lesion', 'art-map']);
    const consoleErrors: string[] = [];
    page.on('console', (message) => {
      // A missing favicon is not the viewer's concern.
      if (message.type() === 'error' && !message.text().includes('Failed to load resource')) {
        consoleErrors.push(message.text());
      }
    });

    await page.goto('/runs/r1');
    await page.getByRole('link', { name: 'View images' }).click();
    await expect(page).toHaveURL(/\/runs\/r1\/view$/);

    await expect(layout(page)).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
    await expect(layout(page)).toHaveAttribute('data-layers', 'template,lesion,disconnection');
    await expect(page.getByRole('group', { name: /Background: CT in MNI space/ })).toBeVisible();
    await expect(page.getByTestId('no-template')).toHaveCount(0);
    const box = await canvas(page).boundingBox();
    expect(box?.width ?? 0).toBeGreaterThan(200);
    expect(box?.height ?? 0).toBeGreaterThan(200);

    // Authenticated download: token in the header only, never in the URL.
    expect(seen.downloads).toHaveLength(3);
    for (const download of seen.downloads) {
      expect(download.authorization).toBe('Bearer e2e-token');
      expect(download.url).not.toContain('e2e-token');
      expect(new URL(download.url).search).toBe('');
    }
    expect(seen.external, 'requests to other origins').toEqual([]);
    expect(consoleErrors, 'console errors').toEqual([]);
  });

  test('opacity and visibility change what is drawn', async ({ page }) => {
    await serveArtefacts(page, ['art-ct', 'art-lesion', 'art-map']);
    await page.goto('/runs/r1/view');
    await expect(layout(page)).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });

    const shot = async () => (await canvas(page).screenshot()).toString('base64');
    const overlays = async (visible: boolean) => {
      for (const name of ['Lesion mask', 'Disconnection map']) {
        const toggle = page.getByRole('group', { name: new RegExp(name) }).getByRole('checkbox', { name: 'Show' });
        await (visible ? toggle.check() : toggle.uncheck());
      }
    };

    await overlays(false);
    await expect.poll(shot, { timeout: 10_000 }).not.toBe('');
    const without = await shot();
    await overlays(true);
    await expect.poll(shot, { message: 'overlays change the picture', timeout: 10_000 }).not.toBe(without);
    const full = await shot();

    // A high threshold hides most of the disconnection map; the picture changes again.
    const threshold = page.getByRole('slider', { name: 'Threshold of Disconnection map' });
    await threshold.evaluate((el: HTMLInputElement) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      setter?.call(el, el.max);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await expect.poll(shot, { message: 'threshold changes the picture', timeout: 10_000 }).not.toBe(full);
  });

  test('without a CT in MNI space the viewer says there is no background', async ({ page }) => {
    await serveArtefacts(page, ['art-lesion', 'art-map']);
    await page.goto('/runs/r1/view');
    await expect(layout(page)).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
    await expect(layout(page)).toHaveAttribute('data-layers', 'lesion,disconnection');
    await expect(page.getByTestId('no-template')).toContainText('No anatomical background');
  });

  test('the Spanish interface has the viewer text', async ({ page }) => {
    await serveArtefacts(page, ['art-lesion']);
    await page.addInitScript(() => window.localStorage.setItem('neuroimagen_language', 'es'));
    await page.goto('/runs/r1/view');
    await expect(page.getByRole('heading', { name: 'Visor de imágenes' })).toBeVisible();
    await expect(layout(page)).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
    await expect(page.getByRole('group', { name: /Máscara de lesión/ })).toBeVisible();
  });
});
