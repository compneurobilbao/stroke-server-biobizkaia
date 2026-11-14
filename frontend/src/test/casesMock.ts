import { vi } from 'vitest';
import type { PipelineSummary, RunDetail, Slot } from '../api/v1';

/** Shapes below follow docs/api/openapi-v1.json (only the fields the UI reads). */

export const USER = {
  id: 'u1',
  email: 'ana@example.org',
  full_name: 'Ana Test',
  role: 'researcher',
  is_active: true,
  storage_used_bytes: 0,
  storage_quota_bytes: null,
};

export const PIPELINE: PipelineSummary = {
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

function impl(id: string, extra: Partial<Slot['implementations'][number]> = {}): Slot['implementations'][number] {
  return {
    id,
    step_name: id,
    step_version: '1.0.0',
    available: true,
    enabled: true,
    is_default: false,
    is_pipeline_default: false,
    stub: false,
    ...extra,
  };
}

export function makeSlots(options: { normalisationMounted?: boolean } = {}): Slot[] {
  return [
    {
      name: 'lesion-intake',
      title: 'Lesion intake and QC',
      position: 0,
      optional: false,
      implementations: [impl('lesion-qc', { is_default: true, is_pipeline_default: true })],
      default_implementation: 'lesion-qc',
    },
    {
      name: 'normalisation',
      title: 'CT lesion to MNI152',
      position: 1,
      optional: false,
      when: { inputs_present: ['ct', 'lesion_native'] },
      implementations: options.normalisationMounted
        ? [impl('ct-lesion-to-mni152', { is_default: true, is_pipeline_default: true })]
        : [],
      default_implementation: 'ct-lesion-to-mni152',
    },
    {
      name: 'functional-disconnection',
      title: 'Functional disconnection',
      position: 2,
      optional: true,
      implementations: [
        impl('fdc-stub', { is_default: true, is_pipeline_default: true, stub: true }),
        impl('fdc-alt', { title: 'Alternative connectome' }),
      ],
      default_implementation: 'fdc-stub',
    },
  ];
}

export function makeRun(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: 'r1',
    case_id: 'c1',
    pipeline_id: 'stroke-lnm',
    pipeline_version: '0.1.0',
    input_set: 'mni-mask',
    status: 'running',
    created_at: '2026-11-14T10:00:00Z',
    slots: [
      { id: 's1', slot: 'lesion-intake', position: 0, optional: false, status: 'selected', implementation_id: 'lesion-qc', step_name: 'lesion-qc', step_version: '1.0.0', chosen_by: 'deployment_default', stub: false },
      { id: 's2', slot: 'normalisation', position: 1, optional: false, status: 'skipped', reason: 'condition_false' },
      { id: 's3', slot: 'functional-disconnection', position: 2, optional: true, status: 'selected', implementation_id: 'fdc-alt', step_name: 'fdc-alt', step_version: '1.0.0', chosen_by: 'user', stub: false },
    ],
    steps: [
      { id: 'a1', run_slot_id: 's1', slot: 'lesion-intake', attempt: 1, trigger: 'initial', status: 'succeeded', queued_at: '2026-11-14T10:00:00Z', log_available: true },
      { id: 'a2', run_slot_id: 's3', slot: 'functional-disconnection', attempt: 1, trigger: 'initial', status: 'running', queued_at: '2026-11-14T10:00:05Z', log_available: false },
    ],
    ...overrides,
  };
}

export interface Call {
  method: string;
  path: string;
  body: BodyInit | null | undefined;
}

export interface MockOptions {
  pipelines?: PipelineSummary[];
  slots?: Slot[];
  run?: RunDetail;
  /** Extra or overriding handlers keyed `METHOD /path` (path without `/api` and query). */
  routes?: Record<string, (init: RequestInit | undefined) => Response>;
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

/** Stubs `fetch` with an in-memory v1 API and returns the recorded calls. */
export function mockApi(options: MockOptions = {}) {
  const calls: Call[] = [];
  const run = options.run ?? makeRun();
  const routes: Record<string, (init: RequestInit | undefined) => Response> = {
    'GET /auth/me': () => jsonResponse(USER),
    'GET /v1/pipelines': () => jsonResponse(options.pipelines ?? [PIPELINE]),
    'GET /v1/pipelines/stroke-lnm/versions/0.1.0/slots': () => jsonResponse(options.slots ?? makeSlots()),
    'GET /v1/cases': () => jsonResponse([]),
    'POST /v1/cases': () =>
      jsonResponse({ id: 'c1', subject_label: 'sub-0001', session_label: 'ses-01', created_at: '2026-11-14T10:00:00Z', updated_at: '2026-11-14T10:00:00Z' }, 201),
    'PUT /v1/cases/c1/inputs/ct': () => jsonResponse({ id: 'i1', role: 'ct', artefact_type: 'ct_native', created_at: '2026-11-14T10:00:00Z' }, 201),
    'PUT /v1/cases/c1/inputs/lesion_native': () => jsonResponse({ id: 'i2', role: 'lesion_native', artefact_type: 'lesion_mask_native', created_at: '2026-11-14T10:00:00Z' }, 201),
    'PUT /v1/cases/c1/inputs/lesion_mni': () => jsonResponse({ id: 'i3', role: 'lesion_mni', artefact_type: 'lesion_mask_mni_any', created_at: '2026-11-14T10:00:00Z' }, 201),
    'POST /v1/cases/c1/runs': () => jsonResponse(run, 201),
    'GET /v1/runs/r1': () => jsonResponse(run),
    'GET /v1/runs/r1/artifacts': () => jsonResponse([]),
    'GET /v1/runs/r1/reports': () => jsonResponse([]),
    ...options.routes,
  };
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    const path = url.replace(/^\/api/, '').split('?')[0];
    const method = init?.method ?? 'GET';
    calls.push({ method, path, body: init?.body });
    const handler = routes[`${method} ${path}`];
    return handler ? handler(init) : jsonResponse({ detail: `unmocked ${method} ${path}`, code: 'unmocked' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls, fetchMock };
}
