import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { errorText } from '../../api/client';
import type { RunSlot, StepAttempt, StepLog } from '../../api/v1';
import { useFormatters } from '../../shared/useFormatters';
import { RunStatusBadge } from './RunStatusBadge';

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

/** Step timeline: one entry per pipeline slot, with its implementation, state, attempts and logs. */
export function RunTimeline({ slots, steps, loadLog }: RunTimelineProps) {
  const { t } = useTranslation();
  const { date } = useFormatters();
  const ordered = [...slots].sort((a, b) => a.position - b.position);
  return (
    <ol className="run-timeline" aria-label={t('cases.run.timeline')}>
      {ordered.map((slot) => {
        const attempts = steps.filter((s) => s.run_slot_id === slot.id).sort((a, b) => a.attempt - b.attempt);
        const state = slotState(slot, attempts);
        return (
          <li key={slot.id} className="timeline-item" data-slot={slot.slot} data-state={state}>
            <div className="timeline-head">
              <strong>{slot.slot}</strong>
              <RunStatusBadge state={state} />
              {slot.optional && <span className="tag">{t('cases.slots.optional')}</span>}
            </div>
            {slot.status === 'selected' && (
              <p className="hint timeline-impl">
                {t('cases.run.implementation')}: <code>{slot.implementation_id ?? slot.step_name}</code>
                {slot.step_version ? ` ${slot.step_version}` : ''}
                {slot.stub ? ` (${t('cases.slots.stub')})` : ''}
                {slot.chosen_by ? ` - ${t(`cases.run.chosenBy.${slot.chosen_by}`)}` : ''}
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
      })}
    </ol>
  );
}
