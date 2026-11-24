import { useTranslation } from 'react-i18next';
import type { DevelopmentStage, PlanSlot, Slot, SlotImplementation } from '../../api/v1';
import { isPlanned, planSlotState } from './plan';
import { upstreamText } from './RunTimeline';
import {
  defaultImplementationId,
  developmentSlotNames,
  implementationTitle,
  isSelectable,
  isSlotUnavailable,
  slotApplies,
} from './slots';

interface SlotSelectorProps {
  slots: readonly Slot[];
  /** Input roles of the chosen wizard mode; slots whose condition is not met are shown as not used. */
  roles: readonly string[];
  value: Record<string, string>;
  onChange: (slot: string, implementationId: string) => void;
  disabled?: boolean;
  /**
   * The plan preview of the chosen analysis: only the slots that take part in it are listed, with
   * their role. Without it every slot of the pipeline is listed (the wizard before F1-22).
   */
  plan?: readonly PlanSlot[] | null;
  /** Slot name to the implementation the chosen preset fixes; shown read-only. */
  fixed?: Record<string, string>;
  /** Stages in development: a slot that is one is greyed and has no selector. */
  development?: readonly DevelopmentStage[];
}

function fixedLabel(t: ReturnType<typeof useTranslation>['t'], slot: Slot, id: string): string {
  const impl = slot.implementations.find((i) => i.id === id);
  return impl ? implementationTitle(t, impl) : id;
}

/**
 * What a candidate implementation is, beyond its name: the external tool it wraps (name, version and
 * licence of the upstream block) and its validation level, with an "experimental" mark for
 * `reference-tested`. Candidates with neither are not listed.
 */
function CandidateBadges({ impls, selected }: { impls: readonly SlotImplementation[]; selected: string }) {
  const { t } = useTranslation();
  const rows = impls.filter((impl) => impl.upstream || impl.validation_level);
  if (rows.length === 0) return null;
  return (
    <ul className="impl-badges" aria-label={t('cases.slots.candidates')}>
      {rows.map((impl) => {
        const external = impl.upstream ? upstreamText(impl.upstream) : '';
        const level = impl.validation_level;
        return (
          <li key={impl.id} data-impl={impl.id} data-current={impl.id === selected ? 'true' : undefined}>
            <span className="impl-name">{implementationTitle(t, impl)}</span>
            {external && (
              <span className="tag tag-warn" data-testid="impl-external">
                {t('cases.slots.externalTool', { tool: external })}
              </span>
            )}
            {level && (
              <span
                className={`tag ${impl.experimental ? 'tag-warn' : level === 'validated' ? '' : 'tag-muted'}`}
                data-testid="impl-level"
                data-level={level}
              >
                {t(`cases.slots.level.${level}`)}
                {impl.experimental ? ` - ${t('cases.slots.experimental')}` : ''}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * One selector per pipeline slot (per planned slot when a plan is given): the default preselected,
 * unavailable slots marked, preset-fixed choices read-only.
 */
export function SlotSelector({
  slots,
  roles,
  value,
  onChange,
  disabled,
  plan,
  fixed = {},
  development = [],
}: SlotSelectorProps) {
  const { t } = useTranslation();
  const developmentSlots = developmentSlotNames(development);
  const planned = plan ? new Map(plan.filter(isPlanned).map((slot): [string, PlanSlot] => [slot.name, slot])) : null;
  const ordered = [...slots]
    .sort((a, b) => a.position - b.position)
    .filter((slot) => !planned || planned.has(slot.name));

  function label(impl: SlotImplementation, isDefault: boolean): string {
    const base = implementationTitle(t, impl);
    const flags = [
      isDefault ? t('cases.slots.default') : '',
      impl.stub ? t('cases.slots.stub') : '',
      impl.experimental ? t('cases.slots.experimental') : '',
    ].filter(Boolean);
    const text = flags.length ? `${base} (${flags.join(', ')})` : base;
    return isSelectable(impl) ? text : `${text} - ${t('cases.slots.unavailable')}`;
  }

  return (
    <ul className="slot-list">
      {ordered.map((slot) => {
        const title = slot.title || slot.name;
        const applies = planned ? true : slotApplies(slot, roles);
        const planSlot = planned?.get(slot.name);
        const role = planSlot ? planSlotState(planSlot) : null;
        const fixedId = fixed[slot.name];
        const inDevelopment = developmentSlots.has(slot.name);
        const unavailable = isSlotUnavailable(slot) && !inDevelopment;
        const defaultId = defaultImplementationId(slot);
        const state = inDevelopment ? 'development' : !applies ? 'unused' : unavailable ? 'unavailable' : 'ok';
        return (
          <li key={slot.name} className="slot-row" data-slot={slot.name} data-state={state}>
            <div className="slot-head">
              <strong>{title}</strong>
              {inDevelopment && <span className="tag tag-muted">{t('cases.development.badge')}</span>}
              {role && role !== 'unavailable' && <span className="tag">{t(`cases.plan.role.${role}`)}</span>}
              {slot.optional && <span className="tag">{t('cases.slots.optional')}</span>}
              {!applies && !inDevelopment && <span className="tag tag-muted">{t('cases.slots.notUsed')}</span>}
              {applies && unavailable && <span className="tag tag-warn">{t('cases.slots.unavailable')}</span>}
            </div>
            {slot.description && <p className="hint slot-description">{slot.description}</p>}
            {applies && !unavailable && !inDevelopment && fixedId && (
              <p className="hint slot-fixed" data-fixed={fixedId}>
                {t('cases.slots.fixed')}:{' '}
                <strong>{fixedLabel(t, slot, fixedId)}</strong>
              </p>
            )}
            {applies && !unavailable && !inDevelopment && !fixedId && (
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
            {applies && !unavailable && !inDevelopment && (
              <CandidateBadges
                impls={fixedId ? slot.implementations.filter((impl) => impl.id === fixedId) : slot.implementations.filter(isSelectable)}
                selected={fixedId ?? value[slot.name] ?? defaultId ?? ''}
              />
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
