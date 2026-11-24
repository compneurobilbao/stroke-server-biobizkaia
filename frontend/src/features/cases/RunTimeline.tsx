import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { errorText } from '../../api/client';
import type { RunSlot, StepAttempt, StepLog, UpstreamInfo } from '../../api/v1';
import { useFormatters } from '../../shared/useFormatters';
import { RunStatusBadge } from './RunStatusBadge';
import { implementationLabel } from './slots';

interface RunTimelineProps {
  slots: readonly RunSlot[];
  steps: readonly StepAttempt[];
  loadLog: (stepRunId: string) => Promise<StepLog>;
}

/** Display state of a slot: its latest attempt, else skipped / unavailable / pending. */
export function slotState(slot: RunSlot, attempts: readonly StepAttempt[]): string {
  if (slot.status === 'skipped') return 'skipped';
  if (slot.status === 'unavailable') return 'unavailable';
  if (attempts.length === 0) return 'pending';
  const latest = [...attempts].sort((a, b) => a.attempt - b.attempt)[attempts.length - 1];
  return latest.status;
}

function AttemptLog({ step, loadLog }: { step: StepAttempt; loadLog: RunTimelineProps['loadLog'] }) {
  const { t } = useTranslation();
  const [log, setLog] = useState<StepLog | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function show() {
    setLoading(true);
    setError('');
    try {
      setLog(await loadLog(step.id));
    } catch (e) {
      setError(errorText(e, t('cases.run.logFailed')));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="attempt-log">
      <button type="button" className="secondary compact" disabled={loading} onClick={() => void show()}>
        {log ? t('cases.run.refreshLog') : t('cases.run.showLog')}
      </button>
      {error && (
        <p className="hint" role="alert">
          {error}
        </p>
      )}
      {log && (
        <>
          {log.truncated && <p className="hint">{t('cases.run.logTruncated')}</p>}
          <pre className="log-box" aria-label={t('cases.run.logLabel', { slot: step.slot, attempt: step.attempt })}>
            {log.text || t('cases.run.logEmpty')}
          </pre>
        </>
      )}
    </div>
  );
}

/** Text for the external tool a slot's implementation wraps: name, version and licence, as far as recorded. */
export function upstreamText(upstream: UpstreamInfo): string {
  const name = [upstream.name, upstream.version].filter((part) => typeof part === 'string' && part).join(' ');
  const licence = typeof upstream.licence === 'string' && upstream.licence ? ` (${upstream.licence})` : '';
  return `${name}${licence}`.trim();
}

/** Step timeline: one entry per planned slot; slots outside the plan are collapsed under "Not part of this plan". */
export function RunTimeline({ slots, steps, loadLog }: RunTimelineProps) {
  const { t } = useTranslation();
  const { date } = useFormatters();
  const ordered = [...slots].sort((a, b) => a.position - b.position);
  const notRequested = ordered.filter((slot) => slot.reason === 'not_requested');
  const shown = ordered.filter((slot) => slot.reason !== 'not_requested');

  function item(slot: RunSlot) {
    const attempts = steps.filter((s) => s.run_slot_id === slot.id).sort((a, b) => a.attempt - b.attempt);
    const state = slotState(slot, attempts);
    const external = slot.status === 'selected' && slot.upstream ? upstreamText(slot.upstream) : '';
    return (
      <li key={slot.id} className="timeline-item" data-slot={slot.slot} data-state={state}>
        <div className="timeline-head">
          <strong>{slot.slot}</strong>
          <RunStatusBadge state={state} />
          {slot.plan_role && <span className="tag">{t(`cases.plan.role.${slot.plan_role}`)}</span>}
          {slot.optional && <span className="tag">{t('cases.slots.optional')}</span>}
          {external && <span className="tag tag-warn">{t('cases.run.externalTool')}</span>}
          {slot.status === 'selected' && slot.experimental && (
            <span className="tag tag-warn" data-testid="experimental">
              {t('cases.run.experimental')}
            </span>
          )}
        </div>
        {slot.status === 'selected' && (
          <p className="hint timeline-impl">
            {t('cases.run.implementation')}:{' '}
            <code>
              {slot.implementation_id
                ? implementationLabel(t, slot.implementation_id, slot.implementation_id)
                : slot.step_name}
            </code>
            {slot.step_version ? ` ${slot.step_version}` : ''}
            {slot.stub ? ` (${t('cases.slots.stub')})` : ''}
            {slot.chosen_by ? ` - ${t(`cases.run.chosenBy.${slot.chosen_by}`)}` : ''}
          </p>
        )}
        {external && (
          <p className="hint timeline-upstream" data-testid="upstream">
            {t('cases.run.upstream', { tool: external })}
          </p>
        )}
        {slot.status !== 'selected' && slot.reason && (
          <p className="hint">{t(`cases.run.reason.${slot.reason}`, { defaultValue: slot.reason })}</p>
        )}
        {attempts.length > 0 && (
          <ul className="attempt-list">
            {attempts.map((step) => (
              <li key={step.id} data-attempt={step.attempt} data-state={step.status}>
                <span>
                  {t('cases.run.attempt', { n: step.attempt })} - {t(`cases.run.trigger.${step.trigger}`)}
                </span>{' '}
                <RunStatusBadge state={step.status} />
                {step.started_at && <span className="muted"> {date(step.started_at)}</span>}
                {step.failure_message && <p className="hint">{step.failure_message}</p>}
                {step.log_available && <AttemptLog step={step} loadLog={loadLog} />}
              </li>
            ))}
          </ul>
        )}
      </li>
    );
  }

  return (
    <ol className="run-timeline" aria-label={t('cases.run.timeline')}>
      {shown.map(item)}
      {notRequested.length > 0 && (
        <li className="timeline-collapsed" data-testid="not-requested">
          <details>
            <summary>{t('cases.run.notRequested', { count: notRequested.length })}</summary>
            <ol className="run-timeline">{notRequested.map(item)}</ol>
          </details>
        </li>
      )}
    </ol>
  );
}
