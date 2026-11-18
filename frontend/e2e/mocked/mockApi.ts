/**
 * Mocked `/api` for the `mocked` Playwright project (cards F0-31 and F1-22).
 *
 * The shapes follow docs/api/openapi-v1.json. Every request is answered from memory through
 * Playwright route interception, so the tests need no backend. The same wizard runs against
 * the real Compose stack in the `real-stack` project (e2e/real/, card F0-31a, e2e/README.md).
 *
 * The plan preview is a small stand-in for the server's planner: enough slots and wiring
 * (functional disconnection needs the lesion intake; normalisation only applies to CT input) to
 * show every slot state the wizard and the run page display. It is not the real resolution
 * algorithm; that one is tested by the backend and by the real-stack specs.
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

interface MockSlot {
  name: string;
  title: string;
  position: number;
  optional: boolean;
  when?: { inputs_present: string[] };
  consumes: unknown[];
  produces: { port: string; type: string }[];
  implementations: ReturnType<typeof impl>[];
  default_implementation: string | null;
}

const SLOTS: MockSlot[] = [
  {
    name: 'lesion-intake',
    title: 'Lesion intake and QC',
    position: 0,
    optional: false,
    consumes: [],
    produces: [{ port: 'lesion_qc', type: 'lesion_qc' }],
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
    produces: [{ port: 'lesion_mask_mni', type: 'lesion_mask_mni' }],
    implementations: [impl('ct-lesion-to-mni152', { is_default: true, is_pipeline_default: true })],
    default_implementation: 'ct-lesion-to-mni152',
  },
  {
    name: 'functional-disconnection',
    title: 'Functional disconnection',
    position: 2,
    optional: true,
    consumes: [],
    produces: [{ port: 'fdisconnection_map', type: 'fdisconnection_map' }],
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
    produces: [{ port: 'profile', type: 'cognet_profile' }],
    // Nothing mounted in this deployment: shown as unavailable, run still starts (optional).
    implementations: [],
    default_implementation: null,
  },
];

const SLOT_IDS: Record<string, string> = {
  'lesion-intake': 's1',
  normalisation: 's2',
  'functional-disconnection': 's3',
  'cognitive-profile': 's4',
};
const ATTEMPT_IDS: Record<string, string> = { 'lesion-intake': 'a1', normalisation: 'a2', 'functional-disconnection': 'a3' };

const preset = (
  id: string,
  title: string,
  inputSets: string[],
  required: string[],
  extra: Record<string, unknown> = {},
) => ({
  id,
  version: '1.0.0',
  sha256: 'a'.repeat(64),
  title,
  description: `${title} (mocked).`,
  input_sets: inputSets,
  targets: { required, if_available: [] as string[] },
  implementations: {},
  is_default: false,
  available: true,
  plans: inputSets.map((input_set) => ({ input_set, available: true, slots: [], reasons: [] })),
  ...extra,
});

/** Presets of the mocked deployment when `presets: true`: two accept the CT mode, two the MNI mode. */
const PRESETS = [
  preset('full', 'Full analysis', ['ct-and-native-mask', 'mni-mask'], ['lesion-intake'], { is_default: true }),
  preset('mni-mask-analysis', 'Disconnection analysis of an MNI lesion mask', ['mni-mask'], ['functional-disconnection']),
  preset('ct-normalisation', 'CT normalisation only', ['ct-and-native-mask'], ['normalisation']),
];

interface PlanBody {
  input_roles: string[];
  preset?: { id: string; version?: string | null } | null;
  targets?: { required: string[]; if_available: string[] } | null;
  implementations?: Record<string, string>;
}

interface Decision {
  name: string;
  status: 'selected' | 'skipped' | 'unavailable';
  reason?: string;
  plan_role?: string;
  required_by?: string | null;
  implementation?: string;
  chosen_by?: string;
}

interface Plan {
  feasible: boolean;
  mode: 'full' | 'preset' | 'free';
  input_set: string;
  preset: { id: string; version: string; sha256: string } | null;
  targets: { required: string[]; if_available: string[] };
  slots: Decision[];
  reasons: Record<string, unknown>[];
}

const applies = (slot: MockSlot, roles: string[]) => {
  const present = slot.when?.inputs_present;
  return !present || present.every((role) => roles.includes(role));
};

function planFor(body: PlanBody, presetsOn: boolean): Plan {
  const roles = body.input_roles;
  const chosenPreset = presetsOn && body.preset ? PRESETS.find((p) => p.id === body.preset?.id) : undefined;
  const targets = body.targets ?? chosenPreset?.targets ?? null;
  const required = new Set(targets?.required ?? []);
  const optional = new Set(targets?.if_available ?? []);
  const reasons: Record<string, unknown>[] = [];
  const roleOf = new Map<string, string>();

  for (const slot of SLOTS) {
    const ok = applies(slot, roles);
    if (!targets) {
      if (ok) roleOf.set(slot.name, slot.optional ? 'if_available' : 'target');
    } else if (required.has(slot.name)) {
      if (ok) roleOf.set(slot.name, 'target');
      else {
        reasons.push({
          code: 'target_not_applicable',
          message: `The target '${slot.name}' does not run with this input set.`,
          target: slot.name,
          slot: slot.name,
        });
      }
    } else if (optional.has(slot.name) && ok) {
      roleOf.set(slot.name, 'if_available');
    }
  }
  // Closure: functional disconnection needs the lesion intake.
  const fd = roleOf.get('functional-disconnection');
  const neededBy = new Map<string, string>();
  if (fd && !roleOf.has('lesion-intake')) {
    roleOf.set('lesion-intake', fd === 'target' ? 'upstream' : 'if_available');
    neededBy.set('lesion-intake', 'functional-disconnection');
  }
  if (targets && roleOf.size === 0 && reasons.length === 0) {
    reasons.push({ code: 'empty_request', message: 'The request has no target that applies.' });
  }

  const slots: Decision[] = SLOTS.map((slot): Decision => {
    const role = roleOf.get(slot.name);
    if (!role) {
      return applies(slot, roles)
        ? { name: slot.name, status: 'skipped', reason: 'not_requested' }
        : { name: slot.name, status: 'skipped', reason: 'condition_false' };
    }
    const wanted = body.implementations?.[slot.name];
    const chosen = wanted ?? slot.default_implementation ?? undefined;
    if (!chosen || slot.implementations.length === 0) {
      if (role !== 'if_available') {
        reasons.push({
          code: 'no_implementation',
          message: `The slot '${slot.name}' has no enabled implementation.`,
          slot: slot.name,
          target: slot.name,
          required_by: neededBy.get(slot.name) ?? slot.name,
        });
      }
      return { name: slot.name, status: 'unavailable', reason: 'no_implementation', plan_role: role };
    }
    return {
      name: slot.name,
      status: 'selected',
      plan_role: role,
      required_by: neededBy.get(slot.name) ?? null,
      implementation: chosen,
      chosen_by: wanted ? 'user' : 'deployment_default',
    };
  });

  const slotTarget = (name: string) => `${name}.${SLOTS.find((s) => s.name === name)?.produces[0]?.port ?? 'out'}`;
  return {
    feasible: reasons.length === 0,
    mode: body.targets ? 'free' : chosenPreset ? 'preset' : 'full',
    input_set: roles.includes('ct') ? 'ct-and-native-mask' : 'mni-mask',
    preset: chosenPreset ? { id: chosenPreset.id, version: chosenPreset.version, sha256: chosenPreset.sha256 } : null,
    targets: { required: [...required].map(slotTarget), if_available: [...optional].map(slotTarget) },
    slots,
    reasons,
  };
}

export interface RecordedRequest {
  method: string;
  path: string;
  body: string | null;
  contentType: string;
}

export interface MockHandle {
  requests: RecordedRequest[];
}

export interface MockOptions {
  /** Offer presets (full analysis, MNI-mask analysis, CT normalisation); default none: the wizard offers the full plan. */
  presets?: boolean;
  /** `free_mode_allowed` of `GET /config`; default false. */
  freeMode?: boolean;
  /** Refuse the first run creation although the preview said feasible (bundles, manifests, parameters). */
  refuseCreateOnce?: boolean;
}

function runDetail(status: 'running' | 'succeeded', plan: Plan, inputs: Array<Record<string, unknown>> = []) {
  const done = status === 'succeeded';
  return {
    id: 'r1',
    case_id: 'c1',
    pipeline_id: 'stroke-lnm',
    pipeline_version: '0.1.0',
    input_set: plan.input_set,
    status,
    created_at: NOW,
    finished_at: done ? NOW : null,
    plan: { mode: plan.mode, preset: plan.preset, targets: plan.targets },
    inputs,
    slots: plan.slots.map((slot, position) => ({
      id: SLOT_IDS[slot.name],
      slot: slot.name,
      position,
      optional: SLOTS[position].optional,
      status: slot.status,
      reason: slot.reason ?? null,
      plan_role: slot.plan_role ?? null,
      ...(slot.status === 'selected'
        ? {
            implementation_id: slot.implementation,
            step_name: slot.implementation,
            step_version: '1.0.0',
            chosen_by: slot.chosen_by,
            stub: slot.implementation === 'fdc-stub',
          }
        : {}),
    })),
    steps: plan.slots
      .filter((slot) => slot.status === 'selected')
      .map((slot) => ({
        id: ATTEMPT_IDS[slot.name],
        run_slot_id: SLOT_IDS[slot.name],
        slot: slot.name,
        attempt: 1,
        trigger: 'initial',
        status: done || slot.name === 'lesion-intake' ? 'succeeded' : 'running',
        queued_at: NOW,
        log_available: slot.name === 'lesion-intake',
      })),
  };
}

/** Installs the mocked API on the page. The run is `running` on its first read and `succeeded` afterwards. */
export async function installMockApi(page: Page, options: MockOptions = {}): Promise<MockHandle> {
  const requests: RecordedRequest[] = [];
  let created: Plan = planFor({ input_roles: ['lesion_mni'] }, false);
  let runReads = 0;
  let createAttempts = 0;
  // Origins declared at upload (multipart field `origin`), by role: shown on the run, as the API does.
  const declared: Record<string, unknown> = {};
  const inputsOf = () => Object.entries(declared).map(([role, origin]) => ({ role, input_file_id: `in-${role}`, origin }));

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
    if (method === 'GET' && path === '/v1/config') {
      return json(route, {
        deployment_profile: options.freeMode ? 'on-premise' : 'research-public',
        run_plan_policy: options.freeMode ? 'free_for_researchers' : 'presets_only',
        free_mode_allowed: Boolean(options.freeMode),
      });
    }
    if (method === 'GET' && path === '/v1/pipelines') return json(route, [PIPELINE]);
    if (method === 'GET' && path === '/v1/pipelines/stroke-lnm/versions/0.1.0/slots') return json(route, SLOTS);
    if (method === 'GET' && path === '/v1/pipelines/stroke-lnm/versions/0.1.0/presets') {
      return json(route, options.presets ? PRESETS : []);
    }
    if (method === 'POST' && path === '/v1/pipelines/stroke-lnm/versions/0.1.0/plan') {
      const body = JSON.parse(request.postData() ?? '{}') as PlanBody;
      if (body.targets && !options.freeMode) {
        return json(route, { detail: 'Free mode is not allowed', code: 'free_mode_not_allowed' }, 403);
      }
      return json(route, planFor(body, Boolean(options.presets)));
    }
    if (method === 'GET' && path === '/v1/cases') return json(route, []);
    if (method === 'POST' && path === '/v1/cases') {
      return json(route, { id: 'c1', subject_label: 'sub-0001', session_label: 'ses-01', created_at: NOW, updated_at: NOW, inputs: [] }, 201);
    }
    const input = /^\/v1\/cases\/c1\/inputs\/([a-z_]+)$/.exec(path);
    if (method === 'PUT' && input) {
      const sent = /name="origin"?
?
([^
]*)/.exec(request.postData() ?? '');
      const origin = sent ? (JSON.parse(sent[1]) as unknown) : null;
      declared[input[1]] = origin;
      return json(route, { id: `in-${input[1]}`, role: input[1], artefact_type: 'x', created_at: NOW, origin }, 201);
    }
    if (method === 'POST' && path === '/v1/cases/c1/runs') {
      createAttempts += 1;
      if (options.refuseCreateOnce && createAttempts === 1) {
        return json(
          route,
          {
            detail: 'The step image is not available',
            code: 'step_contract_too_old',
            slot: 'functional-disconnection',
            reasons: [
              { code: 'step_contract_too_old', message: 'The step image is not available', slot: 'functional-disconnection' },
            ],
          },
          409,
        );
      }
      created = planFor(JSON.parse(request.postData() ?? '{}') as PlanBody, Boolean(options.presets));
      runReads = 0;
      return json(route, runDetail('running', created, inputsOf()), 201);
    }
    if (method === 'GET' && path === '/v1/runs/r1') {
      runReads += 1;
      return json(route, runDetail(runReads > 1 ? 'succeeded' : 'running', created, inputsOf()));
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
