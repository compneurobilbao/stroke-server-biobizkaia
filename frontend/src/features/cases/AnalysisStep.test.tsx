import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../../App';
import type { PlanPreview, PlanReason, PlanSlot, Preset } from '../../api/v1';
import i18n from '../../i18n';
import {
  defaultPlan,
  jsonResponse,
  makePreset,
  makeRun,
  makeSlots,
  mockApi,
  type Call,
} from '../../test/casesMock';
import { TOKEN_KEY } from '../auth/AuthContext';

const mask = () => new File(['x'], 'original-name.nii.gz', { type: 'application/gzip' });
const next = () => userEvent.click(screen.getByRole('button', { name: 'Next' }));
const calls = (all: Call[], method: string, path: string) => all.filter((c) => c.method === method && c.path === path);
const PLAN_PATH = '/v1/pipelines/stroke-lnm/versions/0.1.0/plan';
const RUNS_PATH = '/v1/cases/c1/runs';
const body = (call: Call) => JSON.parse(call.body as string) as Record<string, unknown>;

/** The wizard up to the analysis step, in lesion-mask-in-MNI mode (or CT mode). */
async function openAnalysis(mode: 'mni' | 'ct' = 'mni', state?: unknown) {
  window.localStorage.setItem(TOKEN_KEY, 'jwt-123');
  render(
    <MemoryRouter initialEntries={[{ pathname: '/cases/new', state }]}>
      <App />
    </MemoryRouter>,
  );
  await screen.findByRole('heading', { name: 'New case' });
  const radio = screen.getByRole('radio', {
    name: mode === 'mni' ? /Lesion mask already in MNI/ : /CT and lesion mask/,
  });
  if (!(radio as HTMLInputElement).checked) await userEvent.click(radio);
  await next();
  if (mode === 'mni') {
    await userEvent.upload(screen.getByLabelText('Lesion mask in MNI space (.nii.gz)'), mask());
  } else {
    await userEvent.upload(screen.getByLabelText('CT scan (.nii.gz)'), mask());
    await userEvent.upload(screen.getByLabelText('Lesion mask on the CT (.nii.gz)'), mask());
  }
  await next();
  await screen.findByRole('group', { name: 'Analysis' });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled());
}

const preview = (name: string) => screen.getByRole('region', { name: 'Plan' }).querySelector(`[data-slot="${name}"]`) as HTMLElement;
const startButton = () => screen.getByRole('button', { name: 'Create case and start run' });
const modeRadio = (name: RegExp) => screen.getByRole('radio', { name });
/** Custom depends on /config, which loads independently of the wizard steps. */
const customRadio = () => screen.findByRole('radio', { name: /Custom/ });

const NO_IMPLEMENTATION: PlanReason = {
  code: 'no_implementation',
  message: 'raw server text',
  slot: 'normalisation',
  target: 'normalisation',
  required_by: 'normalisation',
};

function presetList(): Preset[] {
  return [
    makePreset('full', { title: 'Full lesion network analysis', is_default: true, description: 'Every step that applies.' }),
    makePreset('mni-mask-analysis', {
      title: 'Disconnection analysis of an MNI lesion mask',
      version: '1.0.0',
      input_sets: ['mni-mask'],
      targets: { required: ['functional-disconnection'], if_available: [] },
    }),
    makePreset('mni-mask-analysis', {
      title: 'Disconnection analysis of an MNI lesion mask',
      version: '1.1.0',
      input_sets: ['mni-mask'],
      targets: { required: ['functional-disconnection'], if_available: [] },
    }),
    makePreset('ct-normalisation', {
      title: 'Normalisation of a CT lesion to MNI152',
      input_sets: ['ct-and-native-mask'],
      available: false,
      plans: [{ input_set: 'ct-and-native-mask', available: false, slots: [], reasons: [NO_IMPLEMENTATION] }],
    }),
  ];
}

beforeEach(() => {
  window.localStorage.clear();
});

describe('analysis step: presets', () => {
  it('lists the presets that accept the mode, preselects the default and offers neither custom nor full', async () => {
    mockApi({ presets: presetList() });
    await openAnalysis('mni');
    expect(modeRadio(/Full lesion network analysis/)).toBeChecked();
    expect(screen.getByText('Recommended')).toBeInTheDocument();
    expect(modeRadio(/Disconnection analysis of an MNI lesion mask/)).toBeEnabled();
    // one entry per preset id, at its highest version
    expect(screen.getAllByRole('radio', { name: /Disconnection analysis of an MNI lesion mask/ })).toHaveLength(1);
    expect(within(modeRadio(/Disconnection analysis/).closest('label') as HTMLElement).getByText('1.1.0')).toBeInTheDocument();
    // not offered for this mode
    expect(screen.queryByRole('radio', { name: /Normalisation of a CT lesion to MNI152/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('radio', { name: /Custom/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('radio', { name: /Full plan/ })).not.toBeInTheDocument();
  });

  it('shows an unavailable preset disabled with its reason', async () => {
    mockApi({ presets: presetList(), slots: makeSlots({ normalisationMounted: true }) });
    await openAnalysis('ct');
    const radio = modeRadio(/Normalisation of a CT lesion to MNI152/);
    expect(radio).toBeDisabled();
    expect(radio).not.toBeChecked();
    const label = radio.closest('label') as HTMLElement;
    expect(within(label).getByText('Unavailable')).toBeInTheDocument();
    // the reason in plain language, with the slot title instead of its name
    expect(within(label).getByText('CT lesion to MNI152 has no enabled implementation in this deployment.')).toBeInTheDocument();
    expect(modeRadio(/Full lesion network analysis/)).toBeChecked();
  });

  it('offers the full plan when no preset accepts the mode', async () => {
    mockApi({ presets: [] });
    await openAnalysis('mni');
    expect(modeRadio(/Full plan/)).toBeChecked();
    await waitFor(() => expect(preview('lesion-intake')).toBeTruthy());
  });

  it('sends the chosen preset (id and version) to the preview and to the run creation, without targets', async () => {
    const { calls: all } = mockApi({ presets: presetList(), run: makeRun({ input_set: 'mni-mask' }) });
    await openAnalysis('mni');
    await userEvent.click(modeRadio(/Disconnection analysis of an MNI lesion mask/));
    await waitFor(() => {
      const sent = calls(all, 'POST', PLAN_PATH).map(body);
      expect(sent.at(-1)).toMatchObject({ preset: { id: 'mni-mask-analysis', version: '1.1.0' } });
    });
    const last = calls(all, 'POST', PLAN_PATH).map(body).at(-1) as Record<string, unknown>;
    expect(last).toEqual({
      input_roles: ['lesion_mni'],
      implementations: {},
      preset: { id: 'mni-mask-analysis', version: '1.1.0' },
    });
    await next();
    await waitFor(() => expect(startButton()).toBeEnabled());
    await userEvent.click(startButton());
    await screen.findByRole('heading', { name: 'Run' });
    expect(body(calls(all, 'POST', RUNS_PATH)[0])).toEqual({
      pipeline_id: 'stroke-lnm',
      pipeline_version: '0.1.0',
      input_roles: ['lesion_mni'],
      implementations: {},
      preset: { id: 'mni-mask-analysis', version: '1.1.0' },
    });
  });
});

describe('analysis step: custom plan gated by /config', () => {
  it('does not offer Custom when the server says free mode is not allowed to this user', async () => {
    mockApi({ presets: presetList(), config: { free_mode_allowed: false, run_plan_policy: 'presets_only' } });
    await openAnalysis('mni');
    expect(screen.queryByRole('radio', { name: /Custom/ })).not.toBeInTheDocument();
  });

  it('does not offer Custom when /config cannot be read', async () => {
    mockApi({ presets: presetList(), routes: { 'GET /v1/config': () => jsonResponse({ detail: 'x' }, 500) } });
    await openAnalysis('mni');
    expect(screen.queryByRole('radio', { name: /Custom/ })).not.toBeInTheDocument();
  });

  it('offers Custom when allowed: the outputs checklist, a preview per change and the targets in the run request', async () => {
    const { calls: all } = mockApi({
      presets: presetList(),
      config: { free_mode_allowed: true, run_plan_policy: 'free_for_researchers' },
      slots: makeSlots(),
      run: makeRun({ input_set: 'mni-mask' }),
    });
    await openAnalysis('mni');
    await userEvent.click(await customRadio());
    // nothing chosen yet: no plan, and the user cannot go on
    expect(screen.getByText('Choose at least one output to see the plan.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();

    const select = screen.getByRole('combobox', { name: 'Functional disconnection: request' });
    expect(select).toHaveValue('');
    await userEvent.selectOptions(select, 'required');
    await waitFor(() => expect(preview('functional-disconnection')).toBeTruthy());
    expect(preview('functional-disconnection')).toHaveAttribute('data-plan-state', 'target');
    expect(preview('lesion-intake')).toHaveAttribute('data-plan-state', 'upstream');
    expect(preview('lesion-intake')).toHaveTextContent('Added: needed by report');
    expect(preview('normalisation')).toHaveAttribute('data-plan-state', 'condition_false');
    expect(calls(all, 'POST', PLAN_PATH).map(body).at(-1)).toEqual({
      input_roles: ['lesion_mni'],
      implementations: {},
      targets: { required: ['functional-disconnection'], if_available: [] },
    });

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Lesion intake and QC: request' }), 'if_available');
    await waitFor(() =>
      expect(calls(all, 'POST', PLAN_PATH).map(body).at(-1)).toMatchObject({
        targets: { required: ['functional-disconnection'], if_available: ['lesion-intake'] },
      }),
    );

    await next();
    await waitFor(() => expect(startButton()).toBeEnabled());
    await userEvent.click(startButton());
    await screen.findByRole('heading', { name: 'Run' });
    const created = body(calls(all, 'POST', RUNS_PATH)[0]);
    expect(created.targets).toEqual({ required: ['functional-disconnection'], if_available: ['lesion-intake'] });
    expect(created).not.toHaveProperty('preset');
  });

  it('shows the produced artefact types of each slot in the checklist', async () => {
    const slots = makeSlots({ normalisationMounted: true });
    slots[2].produces = [{ port: 'fdisconnection_map', type: 'fdisconnection_map' }];
    mockApi({ presets: [], slots, config: { free_mode_allowed: true } });
    await openAnalysis('mni');
    await userEvent.click(await customRadio());
    const row = screen.getByRole('combobox', { name: 'Functional disconnection: request' }).closest('li') as HTMLElement;
    expect(within(row).getByText('Produces: fdisconnection_map')).toBeInTheDocument();
    expect(within(row).getByText('Optional')).toBeInTheDocument();
  });

  it('sends one preview for two quick changes (debounced)', async () => {
    const { calls: all } = mockApi({ presets: [], config: { free_mode_allowed: true } });
    await openAnalysis('mni');
    await userEvent.click(await customRadio());
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Lesion intake and QC: request' }), 'required');
    await waitFor(() => expect(preview('lesion-intake')).toBeTruthy());
    const before = calls(all, 'POST', PLAN_PATH).length;
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Functional disconnection: request' }), 'required');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Functional disconnection: request' }), 'if_available');
    await waitFor(() => expect(calls(all, 'POST', PLAN_PATH).length).toBeGreaterThan(before));
    await waitFor(() => expect(screen.queryByText('Updating the plan...')).not.toBeInTheDocument());
    expect(calls(all, 'POST', PLAN_PATH).length).toBe(before + 1);
    expect(calls(all, 'POST', PLAN_PATH).map(body).at(-1)).toMatchObject({
      targets: { required: ['lesion-intake'], if_available: ['functional-disconnection'] },
    });
  });
});

const slot = (name: string, extra: Partial<PlanSlot>): PlanSlot => ({ name, status: 'selected', ...extra });

describe('analysis step: plan preview states', () => {
  it('labels each slot as requested, added, if available, not part of the plan, not applicable or unavailable', async () => {
    const answer: PlanPreview = {
      feasible: true,
      mode: 'preset',
      input_set: 'ct-and-native-mask',
      slots: [
        slot('lesion-intake', { plan_role: 'upstream', required_by: 'functional-disconnection', implementation: 'lesion-qc' }),
        slot('normalisation', { status: 'skipped', reason: 'not_requested' }),
        slot('functional-disconnection', { plan_role: 'target', implementation: 'fdc-stub' }),
        slot('cognitive-profile', { plan_role: 'if_available', status: 'unavailable', reason: 'no_implementation' }),
        slot('tract-disconnection', { status: 'skipped', reason: 'condition_false' }),
        slot('cognet-profile', { status: 'skipped', reason: 'upstream_absent' }),
        slot('qc-extra', { plan_role: 'if_available', implementation: 'qc-extra-impl' }),
      ],
      reasons: [],
    };
    const slots = makeSlots();
    mockApi({ presets: [], slots: [...slots, ...['cognitive-profile', 'tract-disconnection', 'cognet-profile', 'qc-extra'].map((name, index) => ({ ...slots[0], name, title: name, position: 10 + index }))], plan: () => answer });
    await openAnalysis('mni');
    await waitFor(() => expect(preview('lesion-intake')).toBeTruthy());
    const state = (name: string) => preview(name).getAttribute('data-plan-state');
    expect(state('lesion-intake')).toBe('upstream');
    expect(preview('lesion-intake')).toHaveTextContent('Added: needed by Functional disconnection');
    expect(state('functional-disconnection')).toBe('target');
    expect(preview('functional-disconnection')).toHaveTextContent('Requested');
    expect(state('qc-extra')).toBe('if_available');
    expect(preview('qc-extra')).toHaveTextContent('If available');
    expect(state('normalisation')).toBe('not_requested');
    expect(preview('normalisation')).toHaveTextContent('Not part of this plan');
    expect(state('tract-disconnection')).toBe('condition_false');
    expect(preview('tract-disconnection')).toHaveTextContent('Does not apply to this input mode');
    expect(state('cognet-profile')).toBe('upstream_absent');
    expect(state('cognitive-profile')).toBe('unavailable');
    expect(preview('cognitive-profile')).toHaveTextContent('Unavailable: no implementation is enabled');
    expect(screen.getByTestId('plan-summary')).toHaveTextContent('This plan can run in this deployment.');
  });

  it('shows every refusal reason in plain language, falls back to the server text, and disables Start', async () => {
    const reasons: PlanReason[] = [
      { code: 'no_implementation', message: 'raw', slot: 'functional-disconnection', target: 'functional-disconnection', required_by: 'report' },
      { code: 'target_not_guaranteed', message: 'raw', target: 'tract-disconnection', slot: 'tract-disconnection' },
      { code: 'target_unreachable', message: 'raw', target: 'report', slot: 'functional-disconnection', port: 'lesion_mask' },
      { code: 'a_future_code', message: 'Text sent by the server for a new code.' },
    ];
    mockApi({
      presets: [],
      plan: () => ({
        feasible: false,
        mode: 'full',
        input_set: 'mni-mask',
        slots: [slot('functional-disconnection', { status: 'unavailable', reason: 'no_implementation', plan_role: 'target' })],
        reasons,
      }),
    });
    await openAnalysis('mni');
    const alert = await screen.findByRole('alert');
    expect(within(alert).getAllByRole('listitem')).toHaveLength(4);
    expect(within(alert).getByText('Functional disconnection has no enabled implementation in this deployment and Research report needs it.')).toBeInTheDocument();
    expect(within(alert).getByText('Tract-wise structural disconnection is not always produced. Ask for it as "If available" instead of "Required".')).toBeInTheDocument();
    expect(within(alert).getByText('Research report cannot be produced here: Functional disconnection needs lesion_mask and no step can provide it with these files.')).toBeInTheDocument();
    expect(within(alert).getByText('Text sent by the server for a new code.')).toBeInTheDocument();
    expect(screen.getByTestId('plan-summary')).toHaveTextContent('This plan cannot run.');

    await next();
    expect(startButton()).toBeDisabled();
    expect(await screen.findByText(/Why this plan cannot run/)).toBeInTheDocument();
  });

  it('enables Start only while the preview of the current request is feasible', async () => {
    const { calls: all } = mockApi({
      presets: presetList(),
      config: { free_mode_allowed: true },
      plan: (request) =>
        request.preset?.id === 'mni-mask-analysis'
          ? { feasible: false, mode: 'preset', input_set: 'mni-mask', slots: [], reasons: [{ code: 'step_contract_too_old', message: 'raw', slot: 'report' }] }
          : defaultPlan(makeSlots(), presetList(), request),
    });
    await openAnalysis('mni');
    await next();
    await waitFor(() => expect(startButton()).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: 'Back' }));
    await userEvent.click(modeRadio(/Disconnection analysis of an MNI lesion mask/));
    await next();
    // the answer of the older request must not leave Start enabled for the new one
    await waitFor(() => expect(calls(all, 'POST', PLAN_PATH)).toHaveLength(2));
    await screen.findByText(/cannot read the run record of this plan/);
    expect(startButton()).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Back' }));
    await userEvent.click(modeRadio(/Full lesion network analysis/));
    await next();
    await waitFor(() => expect(startButton()).toBeEnabled());
  });

  it('shows the error of a preview that fails and keeps Start disabled', async () => {
    mockApi({
      presets: [],
      routes: { [`POST ${PLAN_PATH}`]: () => jsonResponse({ detail: 'Preview is down', code: 'internal' }, 500) },
    });
    await openAnalysis('mni');
    expect(await screen.findByText('Preview is down')).toBeInTheDocument();
    await next();
    expect(startButton()).toBeDisabled();
  });

  it('shows the plain-language reasons in Spanish', async () => {
    await i18n.changeLanguage('es');
    mockApi({
      presets: [],
      plan: () => ({
        feasible: false,
        mode: 'free',
        input_set: 'mni-mask',
        slots: [],
        reasons: [{ code: 'free_mode_not_allowed', message: 'raw' }],
      }),
    });
    window.localStorage.setItem(TOKEN_KEY, 'jwt-123');
    render(
      <MemoryRouter initialEntries={['/cases/new']}>
        <App />
      </MemoryRouter>,
    );
    await screen.findByRole('heading', { name: 'Nuevo caso' });
    await userEvent.click(screen.getByRole('radio', { name: /Máscara de lesión ya en MNI/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Siguiente' }));
    await userEvent.upload(screen.getByLabelText('Máscara de lesión en espacio MNI (.nii.gz)'), mask());
    await userEvent.click(screen.getByRole('button', { name: 'Siguiente' }));
    expect(await screen.findByText(/Elegir sus propios resultados no está permitido/)).toBeInTheDocument();
  });
});

describe('analysis step: implementations within the plan', () => {
  const fixedPlan = (request: { implementations?: Record<string, string> }): PlanPreview => ({
    feasible: true,
    mode: 'preset',
    input_set: 'mni-mask',
    slots: [
      slot('lesion-intake', { plan_role: 'upstream', implementation: 'lesion-qc' }),
      slot('normalisation', { status: 'skipped', reason: 'not_requested' }),
      slot('functional-disconnection', {
        plan_role: 'target',
        implementation: request.implementations?.['functional-disconnection'] ?? 'fdc-alt',
        chosen_by: 'preset',
      }),
    ],
    reasons: [],
  });

  it('lists only the slots of the plan and shows a fixed preset choice read-only', async () => {
    const { calls: all } = mockApi({
      presets: [
        makePreset('fixed-fdc', {
          title: 'Fixed functional disconnection',
          is_default: true,
          input_sets: ['mni-mask'],
          implementations: { 'functional-disconnection': { fixed: 'fdc-alt' } },
        }),
      ],
      plan: fixedPlan,
      run: makeRun({ input_set: 'mni-mask' }),
    });
    await openAnalysis('mni');
    await next();
    const fixed = await screen.findByText(/Fixed by this analysis/);
    expect(fixed.closest('li')).toHaveTextContent('Alternative connectome');
    expect(screen.queryByRole('combobox', { name: 'Functional disconnection' })).not.toBeInTheDocument();
    // the slot outside the plan is not listed, the planned ones carry their role
    expect(screen.queryByText('CT lesion to MNI152')).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Lesion intake and QC' }).closest('li')).toHaveTextContent('Added by the plan');
    await waitFor(() => expect(startButton()).toBeEnabled());
    await userEvent.click(startButton());
    await screen.findByRole('heading', { name: 'Run' });
    expect(body(calls(all, 'POST', RUNS_PATH)[0]).implementations).toEqual({});
  });

  it('preselects the default of the preset and sends only what differs from it', async () => {
    const { calls: all } = mockApi({
      presets: [
        makePreset('alt-default', {
          title: 'Alternative by default',
          is_default: true,
          input_sets: ['mni-mask'],
          implementations: { 'functional-disconnection': { default: 'fdc-alt' } },
        }),
      ],
      plan: fixedPlan,
      run: makeRun({ input_set: 'mni-mask' }),
    });
    await openAnalysis('mni');
    await next();
    const select = await screen.findByRole('combobox', { name: 'Functional disconnection' });
    expect(select).toHaveValue('fdc-alt');
    await userEvent.selectOptions(select, 'fdc-stub');
    await waitFor(() =>
      expect(calls(all, 'POST', PLAN_PATH).map(body).at(-1)).toMatchObject({ implementations: { 'functional-disconnection': 'fdc-stub' } }),
    );
    expect(screen.getByRole('combobox', { name: 'Functional disconnection' })).toHaveValue('fdc-stub');
    // picking the preset's own default again sends nothing
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Functional disconnection' }), 'fdc-alt');
    await waitFor(() => expect(calls(all, 'POST', PLAN_PATH)).toHaveLength(3));
    expect(calls(all, 'POST', PLAN_PATH).map(body).at(-1)?.implementations).toEqual({});
  });
});

describe('create-time refusal', () => {
  it('shows the reasons of a refusal that the preview did not predict and lets the user try again', async () => {
    let attempts = 0;
    const { calls: all } = mockApi({
      presets: [],
      routes: {
        [`POST ${RUNS_PATH}`]: () => {
          attempts += 1;
          return attempts === 1
            ? jsonResponse(
                {
                  detail: 'raw detail',
                  code: 'step_contract_too_old',
                  slot: 'report',
                  reasons: [{ code: 'step_contract_too_old', message: 'raw', slot: 'functional-disconnection' }],
                },
                409,
              )
            : jsonResponse(makeRun({ input_set: 'mni-mask' }), 201);
        },
      },
    });
    await openAnalysis('mni');
    await next();
    await waitFor(() => expect(startButton()).toBeEnabled());
    await userEvent.click(startButton());
    const refusal = await screen.findByTestId('create-error');
    expect(refusal).toHaveTextContent('The server refused to start this run:');
    expect(refusal).toHaveTextContent('The installed version of the step for Functional disconnection cannot read the run record of this plan.');
    expect(screen.queryByText('raw detail')).not.toBeInTheDocument();
    await waitFor(() => expect(startButton()).toBeEnabled());
    await userEvent.click(startButton());
    await screen.findByRole('heading', { name: 'Run' });
    expect(calls(all, 'POST', '/v1/cases')).toHaveLength(1);
  });

  it('shows the server message of a refusal that has no reasons', async () => {
    mockApi({
      presets: [],
      routes: {
        [`POST ${RUNS_PATH}`]: () => jsonResponse({ detail: 'A reference bundle is not available', code: 'bundle_unavailable' }, 409),
      },
    });
    await openAnalysis('mni');
    await next();
    await waitFor(() => expect(startButton()).toBeEnabled());
    await userEvent.click(startButton());
    expect(await screen.findByText('A reference bundle is not available')).toBeInTheDocument();
  });
});

describe('run again with the same plan', () => {
  it('pre-fills a custom plan when the user may still use free mode', async () => {
    mockApi({ presets: presetList(), config: { free_mode_allowed: true } });
    await openAnalysis('mni', {
      rerun: { mode: 'mni', planMode: 'free', presetId: null, custom: { 'functional-disconnection': 'required', 'lesion-intake': 'if_available' } },
    });
    await waitFor(async () => expect(await customRadio()).toBeChecked());
    expect(screen.getByRole('combobox', { name: 'Functional disconnection: request' })).toHaveValue('required');
    expect(screen.getByRole('combobox', { name: 'Lesion intake and QC: request' })).toHaveValue('if_available');
  });

  it('pre-fills the preset, and falls back to the default when it is no longer offered', async () => {
    mockApi({ presets: presetList() });
    await openAnalysis('mni', {
      rerun: { mode: 'mni', planMode: 'preset', presetId: 'mni-mask-analysis', custom: {} },
    });
    expect(modeRadio(/Disconnection analysis of an MNI lesion mask/)).toBeChecked();
  });

  it('falls back to the default when free mode is no longer allowed', async () => {
    mockApi({ presets: presetList(), config: { free_mode_allowed: false } });
    await openAnalysis('mni', {
      rerun: { mode: 'mni', planMode: 'free', presetId: null, custom: { report: 'required' } },
    });
    expect(modeRadio(/Full lesion network analysis/)).toBeChecked();
  });
});

describe('analysis step: optional inputs that will be missing', () => {
  /** The functional disconnection optionally consumes the cognitive profile, a slot a preset can leave out. */
  function slotsWithOptionalProducer() {
    const slots = makeSlots();
    slots[2] = {
      ...slots[2],
      consumes: [
        { port: 'cognet_profile', type: 'cognet_profile', optional: true, from: ['cognitive-profile.profile'] },
        { port: 'lesion_mask', type: 'lesion_mask_mni', optional: true, from: ['inputs.lesion_mni'] },
      ],
    };
    slots.push({
      name: 'cognitive-profile',
      title: 'Cognitive profile',
      position: 3,
      optional: true,
      implementations: [
        {
          id: 'cog',
          step_name: 'cog',
          step_version: '1.0.0',
          available: true,
          enabled: true,
          is_default: true,
          is_pipeline_default: true,
        },
      ],
      default_implementation: 'cog',
    });
    return slots;
  }

  it('tells which optional input of a planned step is missing because its producer is not planned', async () => {
    mockApi({ presets: presetList(), slots: slotsWithOptionalProducer() });
    await openAnalysis('mni');
    // the full plan includes the cognitive profile: nothing is missing
    await waitFor(() => expect(preview('cognitive-profile')).toBeTruthy());
    expect(screen.queryByTestId('plan-missing-inputs')).not.toBeInTheDocument();

    await userEvent.click(modeRadio(/Disconnection analysis of an MNI lesion mask/));
    const note = await screen.findByTestId('plan-missing-inputs');
    expect(note).toHaveTextContent(
      'Functional disconnection will run without the optional input "cognet profile": Cognitive profile will not run in this plan. Add it to the plan to include it.',
    );
    // the supplied lesion mask feeds the other optional port
    expect(note.querySelectorAll('li')).toHaveLength(1);
  });

  it('says nothing about missing inputs for a plan that cannot run', async () => {
    mockApi({
      presets: presetList(),
      slots: slotsWithOptionalProducer(),
      plan: () => ({
        feasible: false,
        mode: 'preset',
        input_set: 'mni-mask',
        slots: [
          { name: 'functional-disconnection', status: 'selected', plan_role: 'target', implementation: 'fdc-stub' },
          { name: 'cognitive-profile', status: 'skipped', reason: 'not_requested' },
        ],
        reasons: [NO_IMPLEMENTATION],
      }),
    });
    await openAnalysis('mni');
    await screen.findByRole('alert');
    expect(screen.queryByTestId('plan-missing-inputs')).not.toBeInTheDocument();
  });
});
