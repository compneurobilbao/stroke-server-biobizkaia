import { describe, expect, it } from 'vitest';
import type { StepAttempt } from '../../api/v1';
import i18n from '../../i18n';
import en from '../../i18n/locales/en.json';
import es from '../../i18n/locales/es.json';
import { failureMessage, runFailure, stepFailure, stepFailureCode } from './failure';

const tr = { t: i18n.t.bind(i18n), i18n } as unknown as Parameters<typeof failureMessage>[0];

function attempt(overrides: Partial<StepAttempt> = {}): StepAttempt {
  return {
    id: 'a1',
    run_slot_id: 's1',
    slot: 'lesion-intake',
    attempt: 1,
    trigger: 'initial',
    status: 'failed',
    queued_at: '2026-11-14T10:00:00Z',
    ...overrides,
  };
}

// Error codes of the steps (their step.yaml manifests), as listed in the user guide (section 9).
const STEP_ERROR_CODES = [
  'input_missing',
  'input_format_invalid',
  'invalid_parameters',
  'lesion_input_missing',
  'conflicting_inputs',
  'mask_not_3d',
  'affine_invalid',
  'mask_empty',
  'mask_empty_after_resampling',
  'space_not_declared',
  'space_mismatch',
  'ct_grid_mismatch',
  'lesion_too_large',
  'bilateral_lesion',
  'lesion_outside_parcellation',
  'mask_outside_atlas_space',
  'disconnection_not_finite',
  'disconnection_map_undefined',
  'disconnection_map_empty',
];

describe('failure messages', () => {
  it('have an en and an es entry for every step error code of the user guide', () => {
    const enMessages = en.cases.failure.message as Record<string, string>;
    const esMessages = es.cases.failure.message as Record<string, string>;
    for (const code of STEP_ERROR_CODES) {
      expect(enMessages[code], code).toBeTruthy();
      expect(esMessages[code], code).toBeTruthy();
      expect(esMessages[code], code).not.toBe(enMessages[code]);
    }
  });

  it('take the code of the step error record before the platform code', () => {
    expect(stepFailureCode(attempt({ failure_code: 'step_failed', step_error: { code: 'mask_empty' } }))).toBe('mask_empty');
    expect(stepFailureCode(attempt({ failure_code: 'timed_out' }))).toBe('timed_out');
    expect(stepFailureCode(attempt())).toBeNull();
  });

  it('translate a known code and fall back to the raw text for an unknown one', async () => {
    expect(failureMessage(tr, 'mask_empty', 'raw')).toBe('The lesion mask has no voxels set.');
    expect(failureMessage(tr, 'a_new_code', 'Raw text from the step.')).toBe('Raw text from the step.');
    expect(failureMessage(tr, null, 'Raw text.')).toBe('Raw text.');
    await i18n.changeLanguage('es');
    expect(failureMessage(tr, 'mask_empty', 'raw')).toBe('La máscara de lesión no tiene ningún vóxel activo.');
  });

  it('describe a rejected step by its error record', () => {
    const view = stepFailure(tr, attempt({ outcome: 'rejected', step_error: { code: 'space_mismatch', message: 'raw step text' } }));
    expect(view).toEqual({
      codes: ['space_mismatch'],
      message: 'The header declares native space, the axes are oblique, or the lesion lies outside the MNI152 extent.',
    });
    expect(stepFailure(tr, attempt({ step_error: { code: 'not_in_catalogue', message: 'raw step text' } }))).toEqual({
      codes: ['not_in_catalogue'],
      message: 'raw step text',
    });
    expect(stepFailure(tr, attempt({ status: 'canceled' }))).toBeNull();
  });

  it('give the run code and the failed step code, with the step message', () => {
    const run = {
      failure_code: 'input_rejected',
      failure_message: "Step 'lesion-intake' did not succeed. The lesion mask has no voxels set.",
      failed_step_run_id: 'a1',
      steps: [attempt({ outcome: 'rejected', step_error: { code: 'mask_empty', message: 'raw' } })],
    };
    expect(runFailure(tr, run, () => 'Lesion intake and QC')).toEqual({
      codes: ['input_rejected', 'mask_empty'],
      message: 'Step "Lesion intake and QC": The lesion mask has no voxels set.',
    });
  });

  it('fall back to the run code message, then to the raw run message', () => {
    expect(runFailure(tr, { failure_code: 'pipeline_unavailable', failure_message: 'raw' }, (s) => s)).toEqual({
      codes: ['pipeline_unavailable'],
      message: 'The pipeline definition of this run is not available in this deployment.',
    });
    expect(runFailure(tr, { failure_code: 'something_new', failure_message: 'Raw run text.' }, (s) => s)).toEqual({
      codes: ['something_new'],
      message: 'Raw run text.',
    });
    expect(runFailure(tr, { failure_code: null, failure_message: null }, (s) => s)).toBeNull();
  });
});
