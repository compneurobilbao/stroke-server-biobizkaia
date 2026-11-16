import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
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
