import { describe, expect, it } from 'vitest';
import type { PlanReason, PlanSlot } from '../../api/v1';
import { PIPELINE, makePreset, makeRun, makeSlots } from '../../test/casesMock';
import {
  CUSTOM_KEY,
  FULL_KEY,
  buildAnalysisOptions,
  buildPlanRequest,
  customTargetLists,
  defaultAnalysisKey,
  inputSetName,
  isOfferedKey,
  isPlanned,
  planReasonsOf,
  planSlotState,
  presetKey,
  reasonKey,
  rerunRequest,
  targetSlotNames,
} from './plan';
import { baselineImplementation, fixedImplementation, implementationOverrides } from './slots';
import { ApiError } from '../../api/client';

const MNI = ['lesion_mni'];

describe('analysis options', () => {
  it('finds the input set of a mode from its roles', () => {
    expect(inputSetName(PIPELINE, MNI)).toBe('mni-mask');
    expect(inputSetName(PIPELINE, ['ct', 'lesion_native'])).toBe('ct-and-native-mask');
    expect(inputSetName(PIPELINE, ['ct'])).toBeNull();
  });

  it('keeps one entry per preset id (highest version) among those accepting the input set', () => {
    const options = buildAnalysisOptions(
      [
        makePreset('a', { version: '1.2.0' }),
        makePreset('a', { version: '1.10.0' }),
        makePreset('a', { version: '1.9.0' }),
        makePreset('ct-only', { input_sets: ['ct-and-native-mask'] }),
      ],
      'mni-mask',
      false,
    );
    expect(options.presets.map((o) => [o.preset.id, o.preset.version])).toEqual([['a', '1.10.0']]);
    expect(options.full).toBe(false);
    expect(options.custom).toBe(false);
  });

  it('takes availability and reasons from the plan of the input set', () => {
    const reason: PlanReason = { code: 'no_implementation', message: 'm', slot: 'normalisation' };
    const preset = makePreset('p', {
      available: true,
      plans: [
        { input_set: 'ct-and-native-mask', available: false, slots: [], reasons: [reason] },
        { input_set: 'mni-mask', available: true, slots: [], reasons: [] },
      ],
    });
    const ct = buildAnalysisOptions([preset], 'ct-and-native-mask', false).presets[0];
    expect(ct.available).toBe(false);
    expect(ct.reasons).toEqual([reason]);
    expect(buildAnalysisOptions([preset], 'mni-mask', false).presets[0].available).toBe(true);
  });

  it('offers the full plan only when no preset accepts the mode, and custom only when allowed', () => {
    expect(buildAnalysisOptions([], 'mni-mask', false)).toMatchObject({ full: true, custom: false });
    expect(buildAnalysisOptions([], 'mni-mask', true)).toMatchObject({ full: true, custom: true });
    expect(buildAnalysisOptions([makePreset('p')], 'mni-mask', true)).toMatchObject({ full: false, custom: true });
    expect(buildAnalysisOptions([makePreset('p')], null, false).presets).toEqual([]);
  });

  it('preselects the default preset, else the first available one, else the full plan, else nothing', () => {
    const unavailable = makePreset('u', { is_default: true, plans: [{ input_set: 'mni-mask', available: false, slots: [] }] });
    const a = makePreset('a');
    const b = makePreset('b', { is_default: true });
    expect(defaultAnalysisKey(buildAnalysisOptions([a, b], 'mni-mask', true))).toBe(presetKey('b'));
    expect(defaultAnalysisKey(buildAnalysisOptions([a, unavailable], 'mni-mask', false))).toBe(presetKey('a'));
    expect(defaultAnalysisKey(buildAnalysisOptions([unavailable], 'mni-mask', true))).toBeNull();
    expect(defaultAnalysisKey(buildAnalysisOptions([], 'mni-mask', false))).toBe(FULL_KEY);
  });

  it('accepts as offered only available presets, the full plan when offered and custom when allowed', () => {
    const unavailable = makePreset('u', { plans: [{ input_set: 'mni-mask', available: false, slots: [] }] });
    const options = buildAnalysisOptions([makePreset('a'), unavailable], 'mni-mask', false);
    expect(isOfferedKey(options, presetKey('a'))).toBe(true);
    expect(isOfferedKey(options, presetKey('u'))).toBe(false);
    expect(isOfferedKey(options, presetKey('missing'))).toBe(false);
    expect(isOfferedKey(options, FULL_KEY)).toBe(false);
    expect(isOfferedKey(options, CUSTOM_KEY)).toBe(false);
    expect(isOfferedKey(options, null)).toBe(false);
    expect(isOfferedKey(buildAnalysisOptions([], 'mni-mask', true), CUSTOM_KEY)).toBe(true);
  });
});

describe('plan requests', () => {
  const slots = makeSlots();

  it('lists custom targets in slot order, required and if available apart', () => {
    expect(
      customTargetLists({ 'functional-disconnection': 'if_available', 'lesion-intake': 'required', normalisation: 'required' }, slots),
    ).toEqual({ required: ['lesion-intake', 'normalisation'], if_available: ['functional-disconnection'] });
  });

  it('builds one body for the preset, custom and full choices, and none when nothing can be asked', () => {
    const options = buildAnalysisOptions([makePreset('p', { version: '2.0.0' })], 'mni-mask', true);
    const impls = { 'functional-disconnection': 'fdc-alt' };
    expect(buildPlanRequest(presetKey('p'), options, {}, slots, MNI, impls)).toEqual({
      input_roles: MNI,
      implementations: impls,
      preset: { id: 'p', version: '2.0.0' },
    });
    expect(buildPlanRequest(CUSTOM_KEY, options, { 'lesion-intake': 'required' }, slots, MNI, {})).toEqual({
      input_roles: MNI,
      implementations: {},
      targets: { required: ['lesion-intake'], if_available: [] },
    });
    expect(buildPlanRequest(CUSTOM_KEY, options, {}, slots, MNI, {})).toBeNull();
    expect(buildPlanRequest(FULL_KEY, options, {}, slots, MNI, {})).toEqual({ input_roles: MNI, implementations: {} });
    expect(buildPlanRequest(null, options, {}, slots, MNI, {})).toBeNull();
    expect(buildPlanRequest(presetKey('missing'), options, {}, slots, MNI, {})).toBeNull();
  });
});

describe('slot states', () => {
  const state = (slot: Partial<PlanSlot>) => planSlotState({ name: 'x', status: 'selected', ...slot });

  it('maps status, role and reason to the label of the preview', () => {
    expect(state({ plan_role: 'target' })).toBe('target');
    expect(state({ plan_role: 'upstream' })).toBe('upstream');
    expect(state({ plan_role: 'if_available' })).toBe('if_available');
    expect(state({ plan_role: null })).toBe('target');
    expect(state({ status: 'skipped', reason: 'not_requested' })).toBe('not_requested');
    expect(state({ status: 'skipped', reason: 'condition_false' })).toBe('condition_false');
    expect(state({ status: 'skipped', reason: 'upstream_absent' })).toBe('upstream_absent');
    expect(state({ status: 'skipped', reason: null })).toBe('not_requested');
    expect(state({ status: 'unavailable', reason: 'no_implementation' })).toBe('unavailable');
  });

  it('counts as planned the selected and the unavailable planned slots only', () => {
    expect(isPlanned({ name: 'x', status: 'selected', plan_role: 'target' })).toBe(true);
    expect(isPlanned({ name: 'x', status: 'unavailable', plan_role: 'if_available' })).toBe(true);
    expect(isPlanned({ name: 'x', status: 'skipped', reason: 'not_requested' })).toBe(false);
    expect(isPlanned({ name: 'x', status: 'skipped', reason: 'condition_false' })).toBe(false);
  });
});

describe('reasons', () => {
  it('reads the reasons of a refused plan from an API error and ignores other errors', () => {
    const reasons = [{ code: 'target_unreachable', message: 'm', slot: 's' }];
    expect(planReasonsOf(new ApiError(422, { detail: 'd', code: 'target_unreachable', reasons }))).toEqual(reasons);
    expect(planReasonsOf(new ApiError(409, { detail: 'd', code: 'bundle_unavailable' }))).toEqual([]);
    expect(planReasonsOf(new ApiError(422, { reasons: [{ nope: true }, null, 'x'] }))).toEqual([]);
    expect(planReasonsOf(new Error('network'))).toEqual([]);
    expect(planReasonsOf(null)).toEqual([]);
  });

  it('uses the plain wording when nothing but the slot itself needs the missing implementation', () => {
    expect(reasonKey({ code: 'no_implementation', message: '', slot: 'a', required_by: 'a' })).toBe('cases.plan.reason.no_implementation_plain');
    expect(reasonKey({ code: 'no_implementation', message: '', slot: 'a' })).toBe('cases.plan.reason.no_implementation_plain');
    expect(reasonKey({ code: 'no_implementation', message: '', slot: 'a', required_by: 'report' })).toBe('cases.plan.reason.no_implementation');
    expect(reasonKey({ code: 'target_unreachable', message: '' })).toBe('cases.plan.reason.target_unreachable');
  });
});

describe('recorded plan of a run', () => {
  it('turns resolved slot.port targets into slot names, required winning over if available', () => {
    expect(targetSlotNames(['a.x', 'a.y', 'b.z'])).toEqual(['a', 'b']);
    expect(targetSlotNames(undefined)).toEqual([]);
    const run = makeRun({
      input_set: 'mni-mask',
      plan: { mode: 'free', preset: null, targets: { required: ['a.x'], if_available: ['a.y', 'b.z'] } },
    });
    expect(rerunRequest(run)).toEqual({ mode: 'mni', planMode: 'free', presetId: null, custom: { a: 'required', b: 'if_available' } });
  });

  it('keeps the preset id, and maps full plans and unknown input sets', () => {
    const preset = makeRun({
      input_set: 'ct-and-native-mask',
      plan: { mode: 'preset', preset: { id: 'full', version: '1.0.0' }, targets: { required: [], if_available: [] } },
    });
    expect(rerunRequest(preset)).toMatchObject({ mode: 'ct', planMode: 'preset', presetId: 'full', custom: {} });
    expect(rerunRequest(makeRun({ input_set: 'other-set', plan: undefined }))).toEqual({
      mode: null,
      planMode: 'full',
      presetId: null,
      custom: {},
    });
  });
});

describe('implementation choices within a plan', () => {
  const slots = makeSlots();
  const preset = makePreset('p', {
    implementations: {
      'functional-disconnection': { default: 'fdc-alt' },
      'lesion-intake': { fixed: 'lesion-qc' },
    },
  });

  it('reads fixed and default choices of a preset', () => {
    expect(fixedImplementation(preset, 'lesion-intake')).toBe('lesion-qc');
    expect(fixedImplementation(preset, 'functional-disconnection')).toBeNull();
    expect(fixedImplementation(null, 'lesion-intake')).toBeNull();
    const fd = slots[2];
    expect(baselineImplementation(fd, preset)).toBe('fdc-alt');
    expect(baselineImplementation(fd, null)).toBe('fdc-stub');
    // a preset default that is not selectable here is ignored
    expect(baselineImplementation(fd, makePreset('q', { implementations: { 'functional-disconnection': { default: 'missing' } } }))).toBe('fdc-stub');
  });

  it('sends the picks that differ from the baseline and never a fixed slot', () => {
    expect(implementationOverrides(slots, { 'functional-disconnection': 'fdc-stub' }, preset)).toEqual({
      'functional-disconnection': 'fdc-stub',
    });
    expect(implementationOverrides(slots, { 'functional-disconnection': 'fdc-alt' }, preset)).toEqual({});
    expect(implementationOverrides(slots, { 'lesion-intake': 'other' }, preset)).toEqual({});
    expect(implementationOverrides(slots, { 'functional-disconnection': 'fdc-alt' }, null)).toEqual({
      'functional-disconnection': 'fdc-alt',
    });
  });
});
