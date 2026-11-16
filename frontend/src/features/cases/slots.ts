import type { Slot, SlotImplementation } from '../../api/v1';

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
