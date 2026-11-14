/**
 * Mocked `/api` for the Playwright tests (mock mode of card F0-31).
 *
 * The shapes follow docs/api/openapi-v1.json. Every request is answered from memory through
 * Playwright route interception, so the tests need no backend. When the Compose `ci` profile
 * is available to this job, the tests are meant to run against it instead (see e2e/README.md).
 */
import type { Page, Route } from '@playwright/test';

export const TOKEN_KEY = 'neuroimagen_access_token';

const NOW = '2026-11-14T10:00:00Z';

const USER = {
  id: 'u1',
  email: 'researcher@example.org',
  full_name: 'E2E Researcher',
  role: 'researcher',
  is_active: true,
  storage_used_bytes: 0,
  storage_quota_bytes: null,
};

const PIPELINE = {
  id: 'stroke-lnm',
  version: '0.1.0',
  title: 'Lesion network mapping v0',
  inputs: [
    { name: 'ct', type: 'ct_native' },
    { name: 'lesion_native', type: 'lesion_mask_native' },
    { name: 'lesion_mni', type: 'lesion_mask_mni_any' },
  ],
  input_sets: [
    { name: 'ct-and-native-mask', inputs: ['ct', 'lesion_native'] },
    { name: 'mni-mask', inputs: ['lesion_mni'] },
  ],
};

const impl = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  step_name: id,
  step_version: '1.0.0',
  available: true,
  enabled: true,
  is_default: false,
  is_pipeline_default: false,
  stub: false,
  ...extra,
});

const SLOTS = [
  {
    name: 'lesion-intake',
    title: 'Lesion intake and QC',
    position: 0,
    optional: false,
    consumes: [],
    produces: [],
    implementations: [impl('lesion-qc', { is_default: true, is_pipeline_default: true })],
    default_implementation: 'lesion-qc',
  },
  {
    name: 'normalisation',
    title: 'CT lesion to MNI152',
    position: 1,
    optional: false,
    when: { inputs_present: ['ct', 'lesion_native'] },
    consumes: [],
    produces: [],
    implementations: [impl('ct-lesion-to-mni152', { is_default: true, is_pipeline_default: true })],
    default_implementation: 'ct-lesion-to-mni152',
  },
  {
    name: 'functional-disconnection',
    title: 'Functional disconnection',
    position: 2,
    optional: true,
    consumes: [],
    produces: [],
    implementations: [
      impl('fdc-stub', { is_default: true, is_pipeline_default: true, stub: true }),
      impl('fdc-alt', { title: 'Alternative connectome' }),
    ],
    default_implementation: 'fdc-stub',
  },
  {
    name: 'cognitive-profile',
    title: 'Cognitive profile',
    position: 3,
    optional: true,
    consumes: [],
    produces: [],
    // Nothing mounted in this deployment: shown as unavailable, run still starts (optional).
    implementations: [],
    default_implementation: null,
  },
];

export interface RecordedRequest {
  method: string;
  path: string;
  body: string | null;
  contentType: string;
}

export interface MockHandle {
  requests: RecordedRequest[];
}

function runDetail(status: 'running' | 'succeeded', overrides: Record<string, unknown>, inputSet: string, chosen: string) {
  const ct = inputSet === 'ct-and-native-mask';
  const done = status === 'succeeded';
  return {
    id: 'r1',
    case_id: 'c1',
    pipeline_id: 'stroke-lnm',
    pipeline_version: '0.1.0',
    input_set: inputSet,
    status,
    created_at: NOW,
    finished_at: done ? NOW : null,
    slots: [
      { id: 's1', slot: 'lesion-intake', position: 0, optional: false, status: 'selected', implementation_id: 'lesion-qc', step_name: 'lesion-qc', step_version: '1.0.0', chosen_by: 'deployment_default', stub: false },
      ct
        ? { id: 's2', slot: 'normalisation', position: 1, optional: false, status: 'selected', implementation_id: 'ct-lesion-to-mni152', step_name: 'ct-lesion-to-mni152', step_version: '1.0.0', chosen_by: 'deployment_default', stub: false }
        : { id: 's2', slot: 'normalisation', position: 1, optional: false, status: 'skipped', reason: 'condition_false' },
      { id: 's3', slot: 'functional-disconnection', position: 2, optional: true, status: 'selected', implementation_id: chosen, step_name: chosen, step_version: '1.0.0', chosen_by: chosen === 'fdc-stub' ? 'deployment_default' : 'user', stub: chosen === 'fdc-stub' },
      { id: 's4', slot: 'cognitive-profile', position: 3, optional: true, status: 'unavailable', reason: 'no_implementation' },
    ],
    steps: [
      { id: 'a1', run_slot_id: 's1', slot: 'lesion-intake', attempt: 1, trigger: 'initial', status: 'succeeded', queued_at: NOW, log_available: true },
      ...(ct ? [{ id: 'a2', run_slot_id: 's2', slot: 'normalisation', attempt: 1, trigger: 'initial', status: done ? 'succeeded' : 'running', queued_at: NOW, log_available: false }] : []),
      { id: 'a3', run_slot_id: 's3', slot: 'functional-disconnection', attempt: 1, trigger: 'initial', status: done ? 'succeeded' : 'running', queued_at: NOW, log_available: false },
    ],
    ...overrides,
  };
}

/** Installs the mocked API on the page. The run is `running` on its first read and `succeeded` afterwards. */
export async function installMockApi(page: Page): Promise<MockHandle> {
  const requests: RecordedRequest[] = [];
  let inputSet = 'mni-mask';
  let chosen = 'fdc-stub';
  let runReads = 0;

  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

  await page.addInitScript((key) => window.localStorage.setItem(key, 'e2e-token'), TOKEN_KEY);

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api/, '');
    const method = request.method();
    requests.push({
      method,
      path,
      body: request.postData(),
      contentType: request.headers()['content-type'] ?? '',
    });

    if (method === 'GET' && path === '/auth/me') return json(route, USER);
    if (method === 'GET' && path === '/v1/pipelines') return json(route, [PIPELINE]);
    if (method === 'GET' && path === '/v1/pipelines/stroke-lnm/versions/0.1.0/slots') return json(route, SLOTS);
    if (method === 'GET' && path === '/v1/cases') return json(route, []);
    if (method === 'POST' && path === '/v1/cases') {
      return json(route, { id: 'c1', subject_label: 'sub-0001', session_label: 'ses-01', created_at: NOW, updated_at: NOW, inputs: [] }, 201);
    }
    const input = /^\/v1\/cases\/c1\/inputs\/([a-z_]+)$/.exec(path);
    if (method === 'PUT' && input) {
      return json(route, { id: `in-${input[1]}`, role: input[1], artefact_type: 'x', created_at: NOW }, 201);
    }
    if (method === 'POST' && path === '/v1/cases/c1/runs') {
      const body = JSON.parse(request.postData() ?? '{}') as { input_roles?: string[]; implementations?: Record<string, string> };
      inputSet = body.input_roles?.includes('ct') ? 'ct-and-native-mask' : 'mni-mask';
      chosen = body.implementations?.['functional-disconnection'] ?? 'fdc-stub';
      runReads = 0;
      return json(route, runDetail('running', {}, inputSet, chosen), 201);
    }
    if (method === 'GET' && path === '/v1/runs/r1') {
      runReads += 1;
      return json(route, runDetail(runReads > 1 ? 'succeeded' : 'running', {}, inputSet, chosen));
    }
    if (method === 'GET' && path === '/v1/runs/r1/artifacts') {
      return json(
        route,
        runReads > 1
          ? [{ id: 'art00001', case_id: 'c1', pipeline_run_id: 'r1', origin: 'step', port: 'lesion_mask_mni', artefact_type: 'lesion_mask_mni', filename: 'out.nii.gz', size_bytes: 2048, synthetic: true, created_at: NOW }]
          : [],
      );
    }
    if (method === 'GET' && path === '/v1/runs/r1/reports') {
      return json(
        route,
        runReads > 1 ? [{ id: 'rep00001', case_id: 'c1', pipeline_run_id: 'r1', step_run_id: 'a3', pdf_artifact_id: 'p1', template: 'research-report', language: 'en', created_at: NOW }] : [],
      );
    }
    if (method === 'GET' && path === '/v1/runs/r1/steps/a1/log') {
      return route.fulfill({ status: 200, contentType: 'text/plain', body: 'lesion-qc: mask checked\n' });
    }
    return json(route, { detail: `unmocked ${method} ${path}`, code: 'unmocked' }, 404);
  });

  return { requests };
}
