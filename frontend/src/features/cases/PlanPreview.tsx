import { useTranslation } from 'react-i18next';
import type { DevelopmentStage, PlanPreview as PlanPreviewData, PlanReason, Slot } from '../../api/v1';
import { missingOptionalInputs, planSlotState, reasonKey, reasonParams } from './plan';
import { developmentSlotNames, slotImplementationName, slotLabel } from './slots';

/** State of the live plan preview of the wizard. */
export interface PreviewState {
  status: 'idle' | 'loading' | 'ready' | 'error';
  /** Latest answer; kept while a newer one is being fetched (shown as out of date). */
  data: PlanPreviewData | null;
  error: string;
}

export const IDLE_PREVIEW: PreviewState = { status: 'idle', data: null, error: '' };

type Translate = ReturnType<typeof useTranslation>['t'];

/** Title of a slot (catalogue, else API title, else the name); reasons and states name slots by title. */
export function slotTitleOf(t: Translate, slots: readonly Slot[]): (name: string) => string {
  const titles = new Map(slots.map((slot): [string, string | null | undefined] => [slot.name, slot.title]));
  return (name) => slotLabel(t, name, titles.get(name));
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
  /** Input roles of the wizard mode: an optional input fed by a supplied file is not reported as missing. */
  roles: readonly string[];
  /** Nothing can be previewed yet: say what is missing. */
  idleHint?: string;
  /** Stages in development: a plan slot that is one is labelled so instead of "does not apply". */
  development?: readonly DevelopmentStage[];
}

/** Each slot of the plan the request would get, with its role or why it is left out, and every refusal reason. */
export function PlanPreview({ preview, slots, roles, idleHint, development = [] }: PlanPreviewProps) {
  const { t } = useTranslation();
  const titleOf = slotTitleOf(t, slots);
  const developmentSlots = developmentSlotNames(development);
  const { data } = preview;
  const position = new Map(slots.map((slot): [string, number] => [slot.name, slot.position]));
  const ordered = [...(data?.slots ?? [])].sort(
    (a, b) => (position.get(a.name) ?? 0) - (position.get(b.name) ?? 0),
  );
  const stale = preview.status === 'loading';
  // Only for a plan that can run: a refused plan has no usable slot list to talk about.
  const missing = data?.feasible ? missingOptionalInputs(slots, data.slots ?? [], roles) : [];

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
              // A slot in development is not "unavailable" or "does not apply": it is not built yet.
              const inDevelopment = developmentSlots.has(slot.name) && slot.status !== 'selected';
              return (
                <li
                  key={slot.name}
                  className="plan-slot"
                  data-slot={slot.name}
                  data-plan-state={state}
                  data-development={inDevelopment ? 'true' : undefined}
                >
                  <strong>{titleOf(slot.name)}</strong>
                  <span className={`tag ${state === 'target' || state === 'upstream' || state === 'if_available' ? '' : 'tag-muted'}`}>
                    {inDevelopment
                      ? t('cases.development.badge')
                      : t(`cases.plan.state.${state}`, { slot: slot.required_by ? titleOf(slot.required_by) : t('cases.plan.thePlan') })}
                  </span>
                  {slot.status === 'selected' && slot.implementation && (
                    <span className="hint">
                      {t('cases.run.implementation')}:{' '}
                      <code data-testid="plan-implementation">{slotImplementationName(t, slots, slot.name, slot.implementation)}</code>
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
          {missing.length > 0 && (
            <div className="plan-missing" data-testid="plan-missing-inputs">
              <strong>{t('cases.plan.missingHeading')}</strong>
              <ul>
                {missing.map((item) => (
                  <li key={`${item.consumer}-${item.port}`} data-consumer={item.consumer} data-port={item.port}>
                    {t('cases.plan.missingInput', {
                      consumer: titleOf(item.consumer),
                      port: item.port.replace(/[_-]/g, ' '),
                      producers: item.producers.map(titleOf).join(' / '),
                    })}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <ReasonList reasons={data.reasons ?? []} titleOf={titleOf} alert heading={t('cases.plan.refused')} />
        </div>
      )}
    </section>
  );
}
