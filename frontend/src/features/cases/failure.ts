import type { useTranslation } from 'react-i18next';
import type { RunDetail, StepAttempt } from '../../api/v1';

/** `t` and `i18n` of `useTranslation` (the helpers take them so they stay testable). */
type Translation = Pick<ReturnType<typeof useTranslation>, 't' | 'i18n'>;

/** A failure as the UI shows it: the codes (machine-readable, as in the logs and the API) and a message. */
export interface FailureView {
  codes: string[];
  message: string;
}

const messageKey = (code: string) => `cases.failure.message.${code}`;

/**
 * Code of a stopped attempt: the step's own error code (error.json `code`, e.g. `mask_empty`) when it
 * wrote one, else the platform's failure code (`step_failed`, `timed_out`, ...).
 */
export function stepFailureCode(step: StepAttempt): string | null {
  const code = step.step_error?.code;
  if (typeof code === 'string' && code) return code;
  return step.failure_code || null;
}

/** Text the step or the platform gave for a stopped attempt (error.json `message`, else the failure message). */
export function stepFailureRaw(step: StepAttempt): string {
  const message = step.step_error?.message;
  if (typeof message === 'string' && message) return message;
  return step.failure_message || '';
}

/** Translated message for a failure code when the catalogue has one, else the raw text. */
export function failureMessage({ t, i18n }: Translation, code: string | null | undefined, raw: string | null | undefined): string {
  if (code && i18n.exists(messageKey(code))) return t(messageKey(code));
  return raw || '';
}

/** What the timeline shows for a stopped attempt; null when it carries neither a code nor a message. */
export function stepFailure(translation: Translation, step: StepAttempt): FailureView | null {
  const code = stepFailureCode(step);
  const message = failureMessage(translation, code, stepFailureRaw(step));
  if (!code && !message) return null;
  return { codes: code ? [code] : [], message };
}

/**
 * What the run page shows for a failed run: the run's code and, when the attempt that made it fail is
 * known, that step's code too. The message names the step and gives the step's translated message
 * when the catalogue has its code; otherwise the run code's message, else the run's raw message.
 */
export function runFailure(
  translation: Translation,
  run: Pick<RunDetail, 'failure_code' | 'failure_message' | 'failed_step_run_id' | 'steps'>,
  slotTitle: (slot: string) => string,
): FailureView | null {
  const { t, i18n } = translation;
  const step = run.failed_step_run_id ? run.steps?.find((attempt) => attempt.id === run.failed_step_run_id) : undefined;
  const stepCode = step ? stepFailureCode(step) : null;
  const codes = [run.failure_code, stepCode].filter(
    (code, index, all): code is string => Boolean(code) && all.indexOf(code) === index,
  );
  if (step && stepCode && i18n.exists(messageKey(stepCode))) {
    return { codes, message: t('cases.failure.inStep', { slot: slotTitle(step.slot), message: t(messageKey(stepCode)) }) };
  }
  const message = failureMessage(translation, run.failure_code, run.failure_message);
  if (codes.length === 0 && !message) return null;
  return { codes, message };
}
