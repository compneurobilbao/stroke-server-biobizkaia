import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import i18n from '../../i18n';
import { makeRun } from '../../test/casesMock';
import { RunTimeline, slotState } from './RunTimeline';

function renderTimeline(run = makeRun(), loadLog = vi.fn(async () => ({ text: 'step output', truncated: false }))) {
  render(<RunTimeline slots={run.slots ?? []} steps={run.steps ?? []} loadLog={loadLog} />);
  return loadLog;
}

function item(slot: string): HTMLElement {
  const found = screen.getByRole('list', { name: 'Step timeline' }).querySelector(`li.timeline-item[data-slot="${slot}"]`);
  if (!found) throw new Error(`no timeline item for ${slot}`);
  return found as HTMLElement;
}

describe('RunTimeline', () => {
  it('marks a slot whose implementation was experimental when the run was created', () => {
    const run = makeRun();
    const slots = (run.slots ?? []).map((s) =>
      s.slot === 'functional-disconnection' ? { ...s, validation_level: 'reference-tested' as const, experimental: true } : s,
    );
    renderTimeline({ ...run, slots });
    expect(within(item('functional-disconnection')).getByTestId('experimental')).toHaveTextContent('Experimental');
    expect(within(item('lesion-intake')).queryByTestId('experimental')).not.toBeInTheDocument();
  });

  it('shows succeeded, skipped and running states with the implementation that ran', () => {
    renderTimeline();
    expect(item('lesion-intake')).toHaveAttribute('data-state', 'succeeded');
    expect(within(item('lesion-intake')).getByText('lesion-qc')).toBeInTheDocument();
    expect(within(item('lesion-intake')).getByText(/deployment default/)).toBeInTheDocument();
    expect(item('normalisation')).toHaveAttribute('data-state', 'skipped');
    expect(within(item('normalisation')).getByText(/not needed for this input mode/)).toBeInTheDocument();
    expect(item('functional-disconnection')).toHaveAttribute('data-state', 'running');
    expect(within(item('functional-disconnection')).getByText('fdc-alt')).toBeInTheDocument();
    expect(within(item('functional-disconnection')).getByText(/chosen by you/)).toBeInTheDocument();
  });

  it('shows failed and canceled attempts and a slot that has not started', () => {
    const run = makeRun({
      status: 'failed',
      steps: [
        { id: 'a1', run_slot_id: 's1', slot: 'lesion-intake', attempt: 1, trigger: 'initial', status: 'failed', queued_at: '2026-11-14T10:00:00Z', failure_message: 'mask is empty' },
        { id: 'a1b', run_slot_id: 's1', slot: 'lesion-intake', attempt: 2, trigger: 'user_resume', status: 'canceled', queued_at: '2026-11-14T10:05:00Z' },
      ],
    });
    renderTimeline(run);
    expect(item('lesion-intake')).toHaveAttribute('data-state', 'canceled');
    expect(within(item('lesion-intake')).getByText('mask is empty')).toBeInTheDocument();
    expect(within(item('lesion-intake')).getByText(/resumed by user/)).toBeInTheDocument();
    expect(item('functional-disconnection')).toHaveAttribute('data-state', 'pending');
  });

  it('shows the error code of a rejected step with its translated message', async () => {
    const run = makeRun({
      status: 'failed',
      steps: [
        {
          id: 'a1',
          run_slot_id: 's1',
          slot: 'lesion-intake',
          attempt: 1,
          trigger: 'initial',
          status: 'failed',
          outcome: 'rejected',
          queued_at: '2026-11-14T10:00:00Z',
          step_error: { code: 'mask_empty', category: 'input_rejected', message: 'Raw text from the step.' },
        },
      ],
    });
    const { unmount } = render(<RunTimeline slots={run.slots ?? []} steps={run.steps ?? []} loadLog={vi.fn()} />);
    let failure = within(item('lesion-intake')).getByTestId('step-failure');
    expect(failure).toHaveTextContent('The lesion mask has no voxels set.');
    expect(within(failure).getByText('mask_empty').tagName).toBe('CODE');
    expect(failure).toHaveTextContent('Code: mask_empty');
    expect(failure).not.toHaveTextContent('Raw text from the step.');
    unmount();

    await i18n.changeLanguage('es');
    render(<RunTimeline slots={run.slots ?? []} steps={run.steps ?? []} loadLog={vi.fn()} />);
    failure = within(screen.getByRole('list', { name: 'Cronología de pasos' }).querySelector('[data-slot="lesion-intake"]') as HTMLElement).getByTestId('step-failure');
    expect(failure).toHaveTextContent('La máscara de lesión no tiene ningún vóxel activo.');
    expect(failure).toHaveTextContent('Código: mask_empty');
  });

  it('shows the code and the raw message of a failure the catalogue does not know', () => {
    const run = makeRun({
      status: 'failed',
      steps: [
        { id: 'a1', run_slot_id: 's1', slot: 'lesion-intake', attempt: 1, trigger: 'initial', status: 'failed', queued_at: '2026-11-14T10:00:00Z', failure_code: 'contract_violation_outputs', failure_message: 'A declared output is missing.' },
      ],
    });
    renderTimeline(run);
    const failure = within(item('lesion-intake')).getByTestId('step-failure');
    expect(failure).toHaveTextContent('A declared output is missing.');
    expect(failure).toHaveTextContent('Code: contract_violation_outputs');
  });

  it('names slots and implementations from the catalogue and keeps the implementation id visible', async () => {
    const run = makeRun();
    run.slots = [
      { id: 's6', slot: 'report', position: 6, optional: false, status: 'selected', implementation_id: 'research-report-es', step_name: 'research-report', step_version: '0.5.0', chosen_by: 'user' },
    ];
    run.steps = [];
    renderTimeline(run);
    expect(within(item('report')).getByText('Research report')).toBeInTheDocument();
    expect(within(item('report')).getByText('Research report (Spanish)')).toBeInTheDocument();
    expect(within(item('report')).getByTestId('implementation-id')).toHaveTextContent('(research-report-es)');
  });

  it('explains the validation levels when a step shows one', () => {
    const run = makeRun();
    renderTimeline(run);
    expect(screen.queryByTestId('validation-help')).not.toBeInTheDocument();
    cleanup();
    const slots = (run.slots ?? []).map((s) =>
      s.slot === 'functional-disconnection' ? { ...s, validation_level: 'reference-tested' as const, experimental: true } : s,
    );
    renderTimeline({ ...run, slots });
    expect(screen.getByTestId('validation-help')).toHaveTextContent(/reference-tested.*validated.*research use only/);
    expect(within(item('functional-disconnection')).getByTestId('experimental')).toHaveAttribute(
      'title',
      expect.stringContaining('signed validation record'),
    );
  });

  it('marks a slot without implementation as unavailable', () => {
    const run = makeRun();
    run.slots = [
      { id: 's9', slot: 'extra', position: 9, optional: true, status: 'unavailable', reason: 'no_implementation' },
    ];
    renderTimeline(run);
    expect(item('extra')).toHaveAttribute('data-state', 'unavailable');
  });

  it('loads an attempt log on demand and notes truncation', async () => {
    const loadLog = vi.fn(async () => ({ text: 'step output', truncated: true }));
    renderTimeline(makeRun(), loadLog);
    expect(within(item('functional-disconnection')).queryByRole('button', { name: 'Show log' })).not.toBeInTheDocument();
    await userEvent.click(within(item('lesion-intake')).getByRole('button', { name: 'Show log' }));
    expect(loadLog).toHaveBeenCalledWith('a1');
    expect(await screen.findByText('step output')).toBeInTheDocument();
    expect(screen.getByText('The beginning of this log is not shown.')).toBeInTheDocument();
  });
});

describe('slotState', () => {
  it('uses the latest attempt', () => {
    const run = makeRun();
    const slot = run.slots![0];
    const attempts = [
      { ...run.steps![0], attempt: 1, status: 'failed' as const },
      { ...run.steps![0], attempt: 2, status: 'running' as const },
    ];
    expect(slotState(slot, attempts)).toBe('running');
    expect(slotState(slot, [])).toBe('pending');
  });
});
