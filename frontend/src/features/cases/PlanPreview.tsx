import { useTranslation } from 'react-i18next';
import type { PlanPreview as PlanPreviewData, PlanReason, Slot } from '../../api/v1';
import { planSlotState, reasonKey, reasonParams } from './plan';

/** State of the live plan preview of the wizard. */
export interface PreviewState {
  status: 'idle' | 'loading' | 'ready' | 'error';
  /** Latest answer; kept while a newer one is being fetched (shown as out of date). */
  data: PlanPreviewData | null;
  error: string;
}

export const IDLE_PREVIEW: PreviewState = { status: 'idle', data: null, error: '' };

/** Title of a slot (or the name when unknown); reasons and states name slots by title. */
export function slotTitleOf(slots: readonly Slot[]): (name: string) => string {
  const titles = new Map(slots.map((slot): [string, string] => [slot.name, slot.title || slot.name]));
  return (name) => titles.get(name) ?? name;
}

interface ReasonListProps {
  reasons: readonly PlanReason[];
  titleOf: (name: string) => string;
  /** Alert role for reasons that block starting; plain list for hints (unavailable presets). */
  alert?: boolean;
  heading?: string;
}

/** Refusal reasons in plain language (catalogue by code); a code the catalogue lacks shows the server text. */
export function ReasonList({ reasons, titleOf, alert = false, heading }: ReasonListProps) {
  const { t } = useTranslation();
  if (reasons.length === 0) return null;
  return (
    <div className="plan-reasons" role={alert ? 'alert' : undefined}>
      {heading && <strong>{heading}</strong>}
      <ul>
        {reasons.map((reason, index) => (
          <li key={`${reason.code}-${reason.slot ?? ''}-${reason.target ?? ''}-${index}`} data-code={reason.code}>
            {t(reasonKey(reason), {
              defaultValue: reason.message,
              ...reasonParams(reason, titleOf),
            })}
          </li>
        ))}
      </ul>
    </div>
  );
}

interface PlanPreviewProps {
  preview: PreviewState;
  slots: readonly Slot[];
  /** Nothing can be previewed yet: say what is missing. */
  idleHint?: string;
}

/** Each slot of the plan the request would get, with its role or why it is left out, and every refusal reason. */
export function PlanPreview({ preview, slots, idleHint }: PlanPreviewProps) {
  const { t } = useTranslation();
  const titleOf = slotTitleOf(slots);
  const { data } = preview;
  const position = new Map(slots.map((slot): [string, number] => [slot.name, slot.position]));
  const ordered = [...(data?.slots ?? [])].sort(
    (a, b) => (position.get(a.name) ?? 0) - (position.get(b.name) ?? 0),
  );
  const stale = preview.status === 'loading';

  return (
    <section
      className="plan-preview"
      aria-label={t('cases.plan.title')}
      data-state={preview.status}
      data-feasible={data ? String(data.feasible) : undefined}
    >
      <h3>{t('cases.plan.title')}</h3>
      {preview.status === 'idle' && <p className="hint">{idleHint ?? t('cases.plan.idle')}</p>}
      {stale && (
        <p className="hint" role="status">
          {t('cases.plan.updating')}
        </p>
      )}
      {preview.status === 'error' && (
        <p className="hint" role="alert">
          {preview.error}
        </p>
      )}
      {data && (
        <div className={stale ? 'plan-body stale' : 'plan-body'}>
          <p className="hint" data-testid="plan-summary">
            {data.feasible ? t('cases.plan.feasible') : t('cases.plan.infeasible')}
          </p>
          <ul className="plan-list">
            {ordered.map((slot) => {
              const state = planSlotState(slot);
              return (
                <li key={slot.name} className="plan-slot" data-slot={slot.name} data-plan-state={state}>
                  <strong>{titleOf(slot.name)}</strong>
                  <span className={`tag ${state === 'target' || state === 'upstream' || state === 'if_available' ? '' : 'tag-muted'}`}>
                    {t(`cases.plan.state.${state}`, { slot: slot.required_by ? titleOf(slot.required_by) : t('cases.plan.thePlan') })}
                  </span>
                  {slot.status === 'selected' && slot.implementation && (
                    <span className="hint">
                      {t('cases.run.implementation')}: <code>{slot.implementation}</code>
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
          <ReasonList reasons={data.reasons ?? []} titleOf={titleOf} alert heading={t('cases.plan.refused')} />
        </div>
      )}
    </section>
  );
}
