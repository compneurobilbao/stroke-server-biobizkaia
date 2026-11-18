import type {
  PipelineSummary,
  PlanReason,
  PlanRequest,
  PlanSlot,
  PlanTargets,
  Preset,
  RunDetail,
  Slot,
} from '../../api/v1';

/** Helpers of the analysis step: preset options, plan requests, slot states and refusal reasons. */

export type TargetKind = 'required' | 'if_available';
/** Custom (free mode) selection: slot name to how it is wanted; a slot that is absent is not requested. */
export type CustomTargets = Record<string, TargetKind>;

export const CUSTOM_KEY = 'custom';
export const FULL_KEY = 'full';
export const presetKey = (id: string) => `preset:${id}`;

export interface PresetOption {
  key: string;
  preset: Preset;
  available: boolean;
  reasons: PlanReason[];
}

export interface AnalysisOptions {
  /** One entry per preset id that accepts the mode's input set (its highest enabled version). */
  presets: PresetOption[];
  /** The full plan, offered only when no preset accepts the mode (design 2.4, "Default preset"). */
  full: boolean;
  /** Free mode: offered only when `GET /config` says the user may use it. */
  custom: boolean;
}

function compareVersions(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true });
}

/** Name of the input set whose inputs are exactly `roles`, or null. */
export function inputSetName(pipeline: PipelineSummary, roles: readonly string[]): string | null {
  const found = pipeline.input_sets.find(
    (set) => set.inputs.length === roles.length && roles.every((role) => set.inputs.includes(role)),
  );
  return found?.name ?? null;
}

export function buildAnalysisOptions(
  presets: readonly Preset[],
  inputSet: string | null,
  customAllowed: boolean,
): AnalysisOptions {
  const best = new Map<string, Preset>();
  for (const preset of presets) {
    if (inputSet === null || !preset.input_sets.includes(inputSet)) continue;
    const current = best.get(preset.id);
    if (!current || compareVersions(preset.version, current.version) > 0) best.set(preset.id, preset);
  }
  const options: PresetOption[] = [...best.values()].map((preset) => {
    const plan = preset.plans.find((p) => p.input_set === inputSet);
    return {
      key: presetKey(preset.id),
      preset,
      available: plan ? plan.available : preset.available,
      reasons: plan?.reasons ?? [],
    };
  });
  return { presets: options, full: options.length === 0, custom: customAllowed };
}

/** The preselected analysis: the deployment's default preset when it can run, else the first that can. */
export function defaultAnalysisKey(options: AnalysisOptions): string | null {
  const usable = options.presets.filter((option) => option.available);
  const chosen = usable.find((option) => option.preset.is_default) ?? usable[0];
  if (chosen) return chosen.key;
  return options.full ? FULL_KEY : null;
}

export function isOfferedKey(options: AnalysisOptions, key: string | null): boolean {
  if (key === null) return false;
  if (key === FULL_KEY) return options.full;
  if (key === CUSTOM_KEY) return options.custom;
  return options.presets.some((option) => option.key === key && option.available);
}

/** Slots selected in the custom checklist, as the request body wants them (slot order). */
export function customTargetLists(custom: CustomTargets, slots: readonly Slot[]): PlanTargets {
  const ordered = [...slots].sort((a, b) => a.position - b.position);
  return {
    required: ordered.filter((slot) => custom[slot.name] === 'required').map((slot) => slot.name),
    if_available: ordered.filter((slot) => custom[slot.name] === 'if_available').map((slot) => slot.name),
  };
}

/**
 * The plan request of the current analysis choice, or null when nothing can be asked yet (no choice,
 * or a custom plan without any output). The preview and the run creation send the same body.
 */
export function buildPlanRequest(
  key: string | null,
  options: AnalysisOptions,
  custom: CustomTargets,
  slots: readonly Slot[],
  roles: readonly string[],
  implementations: Record<string, string>,
): PlanRequest | null {
  if (key === null) return null;
  const base = { input_roles: [...roles], implementations };
  if (key === FULL_KEY) return base;
  if (key === CUSTOM_KEY) {
    const targets = customTargetLists(custom, slots);
    if (targets.required.length === 0 && targets.if_available.length === 0) return null;
    return { ...base, targets };
  }
  const option = options.presets.find((o) => o.key === key);
  if (!option) return null;
  return { ...base, preset: { id: option.preset.id, version: option.preset.version } };
}

/** Why a slot is, or is not, in a plan (design 3.6): what the preview and the selector label show. */
export type PlanSlotState =
  | 'target'
  | 'upstream'
  | 'if_available'
  | 'not_requested'
  | 'condition_false'
  | 'upstream_absent'
  | 'unavailable';

export function planSlotState(slot: PlanSlot): PlanSlotState {
  if (slot.status === 'selected') return slot.plan_role ?? 'target';
  if (slot.status === 'unavailable') return 'unavailable';
  if (slot.reason === 'condition_false') return 'condition_false';
  if (slot.reason === 'upstream_absent') return 'upstream_absent';
  return 'not_requested';
}

/** A slot that takes part in the plan (chosen implementation, or planned but unavailable). */
export const isPlanned = (slot: PlanSlot): boolean => {
  const state = planSlotState(slot);
  return state === 'target' || state === 'upstream' || state === 'if_available' || state === 'unavailable';
};

/** Slot names of resolved `slot.port` targets (or plain slot names), without duplicates. */
export function targetSlotNames(targets: readonly string[] | undefined): string[] {
  return [...new Set((targets ?? []).map((target) => target.split('.')[0]))];
}

/** Recorded request of a run, as the wizard can pre-fill it ("Run again with the same plan"). */
export interface RerunRequest {
  /** 'ct' or 'mni', from the run's input set when it is one the wizard knows. */
  mode: 'ct' | 'mni' | null;
  planMode: 'full' | 'preset' | 'free';
  presetId: string | null;
  custom: CustomTargets;
}

export function rerunRequest(run: RunDetail): RerunRequest {
  const plan = run.plan;
  const custom: CustomTargets = {};
  if (plan?.mode === 'free') {
    for (const name of targetSlotNames(plan.targets?.if_available)) custom[name] = 'if_available';
    for (const name of targetSlotNames(plan.targets?.required)) custom[name] = 'required';
  }
  return {
    mode: run.input_set === 'mni-mask' ? 'mni' : run.input_set === 'ct-and-native-mask' ? 'ct' : null,
    planMode: plan?.mode ?? 'full',
    presetId: plan?.mode === 'preset' ? (plan.preset?.id ?? null) : null,
    custom,
  };
}

/** Values of a reason's `{{...}}` placeholders; slot and target names become slot titles when known. */
export function reasonParams(reason: PlanReason, titleOf: (name: string) => string): Record<string, string> {
  const title = (name: string | null | undefined) => (name ? titleOf(name) : '');
  return {
    target: title(reason.target),
    slot: title(reason.slot),
    port: reason.port ?? '',
    requiredBy: title(reason.required_by),
  };
}

/** The `reasons` of an API error body (a refused plan), or an empty list for any other error. */
export function planReasonsOf(error: unknown): PlanReason[] {
  const payload = (error as { payload?: unknown } | null)?.payload;
  const reasons = (payload as { reasons?: unknown } | null | undefined)?.reasons;
  if (!Array.isArray(reasons)) return [];
  return reasons.filter((r): r is PlanReason => {
    const reason = r as Partial<PlanReason> | null;
    return reason !== null && typeof reason?.code === 'string' && typeof reason.message === 'string';
  });
}

/** Catalogue key of a reason: a missing implementation of a slot that nothing else needs reads more simply. */
export function reasonKey(reason: PlanReason): string {
  const plain = reason.code === 'no_implementation' && (!reason.required_by || reason.required_by === reason.slot);
  return `cases.plan.reason.${plain ? 'no_implementation_plain' : reason.code}`;
}

/** An optional input of a planned slot that will be missing because no slot that could produce it is planned. */
export interface MissingInput {
  /** Slot that consumes the port. */
  consumer: string;
  port: string;
  /** Slots that could have produced it (alternatives), none of them planned. */
  producers: string[];
}

/**
 * Optional inputs of the planned slots whose producers are all outside the plan (design 3.6: "the report will
 * not include the COGNET profile"). A port fed by an input the user supplied, or by the platform, is not
 * missing; a port fed only by supplied files that were not given is not reported either (the user chose the
 * files, not the plan), nor is one whose producers do not apply to these files.
 */
export function missingOptionalInputs(
  slots: readonly Slot[],
  plan: readonly PlanSlot[],
  roles: readonly string[],
): MissingInput[] {
  const planned = new Set(plan.filter((slot) => slot.status === 'selected').map((slot) => slot.name));
  // A producer that cannot apply to these files is not something the user could add to the plan.
  const inapplicable = new Set(
    plan.filter((slot) => slot.status === 'skipped' && slot.reason === 'condition_false').map((slot) => slot.name),
  );
  const result: MissingInput[] = [];
  for (const slot of [...slots].sort((a, b) => a.position - b.position)) {
    if (!planned.has(slot.name)) continue;
    for (const port of slot.consumes ?? []) {
      if (!port.optional || !port.from || port.from.length === 0) continue;
      const producers: string[] = [];
      let fed = false;
      for (const source of port.from) {
        const [head, tail = ''] = source.split('.', 2);
        if (head === 'inputs') fed ||= roles.includes(tail);
        else if (head === 'run') fed = true;
        else if (planned.has(head)) fed = true;
        else if (!inapplicable.has(head) && !producers.includes(head)) producers.push(head);
      }
      if (!fed && producers.length > 0) result.push({ consumer: slot.name, port: port.port, producers });
    }
  }
  return result;
}
