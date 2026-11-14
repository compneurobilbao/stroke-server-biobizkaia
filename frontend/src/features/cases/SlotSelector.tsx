import { useTranslation } from 'react-i18next';
import type { Slot, SlotImplementation } from '../../api/v1';
import { defaultImplementationId, isSelectable, isSlotUnavailable, slotApplies } from './slots';

interface SlotSelectorProps {
  slots: readonly Slot[];
  /** Input roles of the chosen wizard mode; slots whose condition is not met are shown as not used. */
  roles: readonly string[];
  value: Record<string, string>;
  onChange: (slot: string, implementationId: string) => void;
  disabled?: boolean;
}

/** One selector per pipeline slot: deployment default preselected, unavailable slots marked. */
export function SlotSelector({ slots, roles, value, onChange, disabled }: SlotSelectorProps) {
  const { t } = useTranslation();
  const ordered = [...slots].sort((a, b) => a.position - b.position);

  function label(impl: SlotImplementation, isDefault: boolean): string {
    const base = impl.title || `${impl.step_name} ${impl.step_version}`;
    const flags = [isDefault ? t('cases.slots.default') : '', impl.stub ? t('cases.slots.stub') : ''].filter(Boolean);
    const text = flags.length ? `${base} (${flags.join(', ')})` : base;
    return isSelectable(impl) ? text : `${text} - ${t('cases.slots.unavailable')}`;
  }

  return (
    <ul className="slot-list">
      {ordered.map((slot) => {
        const title = slot.title || slot.name;
        const applies = slotApplies(slot, roles);
        const unavailable = isSlotUnavailable(slot);
        const defaultId = defaultImplementationId(slot);
        const state = !applies ? 'unused' : unavailable ? 'unavailable' : 'ok';
        return (
          <li key={slot.name} className="slot-row" data-slot={slot.name} data-state={state}>
            <div className="slot-head">
              <strong>{title}</strong>
              {slot.optional && <span className="tag">{t('cases.slots.optional')}</span>}
              {!applies && <span className="tag tag-muted">{t('cases.slots.notUsed')}</span>}
              {applies && unavailable && <span className="tag tag-warn">{t('cases.slots.unavailable')}</span>}
            </div>
            {slot.description && <p className="hint slot-description">{slot.description}</p>}
            {applies && !unavailable && (
              <select
                aria-label={title}
                value={value[slot.name] ?? defaultId ?? ''}
                disabled={disabled}
                onChange={(event) => onChange(slot.name, event.target.value)}
              >
                {slot.implementations.map((impl) => (
                  <option key={impl.id} value={impl.id} disabled={!isSelectable(impl)}>
                    {label(impl, impl.id === defaultId)}
                  </option>
                ))}
              </select>
            )}
            {applies && unavailable && (
              <p className="hint slot-note">
                {slot.optional ? t('cases.slots.unavailableOptional') : t('cases.slots.unavailableRequired')}
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
}
