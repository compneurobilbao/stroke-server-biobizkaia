import type { useTranslation } from 'react-i18next';
import type { DevelopmentStage, Preset, Slot, SlotImplementation } from '../../api/v1';

/** An implementation the user may pick: present in the deployment, enabled and loadable. */
export const isSelectable = (impl: SlotImplementation) => impl.available && impl.enabled;

/** True when no implementation of the slot can run in this deployment. */
export const isSlotUnavailable = (slot: Slot) => !slot.implementations.some(isSelectable);

/** True when the slot takes part in a run whose input roles are `roles` (its `when.inputs_present` is met). */
export function slotApplies(slot: Slot, roles: readonly string[]): boolean {
  const present = (slot.when as { inputs_present?: unknown } | null | undefined)?.inputs_present;
  if (!Array.isArray(present)) return true;
  return present.every((role) => typeof role === 'string' && roles.includes(role));
}

/** Deployment default of the slot (else the pipeline default) among the selectable implementations. */
export function defaultImplementationId(slot: Slot): string | null {
  const selectable = slot.implementations.filter(isSelectable);
  return (
    selectable.find((impl) => impl.is_default)?.id ??
    selectable.find((impl) => impl.id === slot.default_implementation)?.id ??
    selectable[0]?.id ??
    null
  );
}

/** Preselected choice for every slot that has a selectable implementation. */
export function defaultChoices(slots: readonly Slot[]): Record<string, string> {
  const choices: Record<string, string> = {};
  for (const slot of slots) {
    const id = defaultImplementationId(slot);
    if (id) choices[slot.name] = id;
  }
  return choices;
}

/** Only the slots whose choice differs from the default are sent, so that the run records the default as such. */
export function overrides(slots: readonly Slot[], choices: Record<string, string>): Record<string, string> {
  const result: Record<string, string> = {};
  for (const slot of slots) {
    const chosen = choices[slot.name];
    if (chosen && chosen !== defaultImplementationId(slot)) result[slot.name] = chosen;
  }
  return result;
}

/** A required slot that applies but cannot run: the server would refuse the run (409 no_implementation). */
export function blockingSlots(slots: readonly Slot[], roles: readonly string[]): Slot[] {
  return slots.filter((slot) => !slot.optional && slotApplies(slot, roles) && isSlotUnavailable(slot));
}

/** Implementation a preset fixes for the slot (the user cannot change it), if any. */
export const fixedImplementation = (preset: Preset | null | undefined, slot: string): string | null =>
  preset?.implementations?.[slot]?.fixed ?? null;

/** What the slot gets when the user picks nothing: a preset's default, else the deployment's default. */
export function baselineImplementation(slot: Slot, preset: Preset | null | undefined): string | null {
  const presetDefault = preset?.implementations?.[slot.name]?.default;
  if (presetDefault && slot.implementations.some((impl) => impl.id === presetDefault && isSelectable(impl))) {
    return presetDefault;
  }
  return defaultImplementationId(slot);
}

/** Explicit user picks that differ from what the slot would get anyway; fixed slots are never sent. */
export function implementationOverrides(
  slots: readonly Slot[],
  picks: Record<string, string>,
  preset: Preset | null | undefined,
): Record<string, string> {
  const result: Record<string, string> = {};
  for (const slot of slots) {
    const picked = picks[slot.name];
    if (!picked || fixedImplementation(preset, slot.name)) continue;
    if (picked !== baselineImplementation(slot, preset)) result[slot.name] = picked;
  }
  return result;
}

/** Translate function of `useTranslation` (the pure helpers here take it so they stay testable). */
type Translate = ReturnType<typeof useTranslation>['t'];

/**
 * Display name of an implementation: the catalogue entry for its id (en and es, so the two cohort
 * entries of one step image read "flnm-fast · HCP Aging" / "flnm-fast · HCP Young Adult"), else the
 * `fallback` (the API title, which the registry or the step manifest gives).
 */
export function implementationLabel(t: Translate, id: string, fallback: string): string {
  return t(`cases.implementation.title.${id}`, { defaultValue: fallback });
}

/** Display name of a listed implementation. */
export function implementationTitle(t: Translate, impl: SlotImplementation): string {
  return implementationLabel(t, impl.id, impl.title || `${impl.step_name} ${impl.step_version}`);
}

/** Names of the slots that are in development in this deployment (stages with a `slot`). */
export function developmentSlotNames(stages: readonly DevelopmentStage[]): Set<string> {
  return new Set(stages.flatMap((stage) => (stage.slot ? [stage.slot] : [])));
}

/**
 * Wizard modes that cannot be used because a slot they need is in development (a non-optional slot
 * that applies to the mode's input roles): shown greyed and not selectable.
 */
export function developmentBlocked(
  slots: readonly Slot[],
  stages: readonly DevelopmentStage[],
  roles: readonly string[],
): boolean {
  const names = developmentSlotNames(stages);
  return slots.some((slot) => names.has(slot.name) && !slot.optional && slotApplies(slot, roles));
}

/** Display name of the implementation a plan or run slot uses: its listed title, else the catalogue or the id. */
export function slotImplementationName(
  t: Translate,
  slots: readonly Slot[],
  slotName: string,
  implementationId: string,
): string {
  const impl = slots.find((slot) => slot.name === slotName)?.implementations.find((i) => i.id === implementationId);
  return impl ? implementationTitle(t, impl) : implementationLabel(t, implementationId, implementationId);
}
