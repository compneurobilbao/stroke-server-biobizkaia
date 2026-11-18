import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../../App';
import type { RunDetail } from '../../api/v1';
import { makeRun, mockApi } from '../../test/casesMock';
import { TOKEN_KEY } from '../auth/AuthContext';

function renderRun() {
  window.localStorage.setItem(TOKEN_KEY, 'jwt-123');
  return render(
    <MemoryRouter initialEntries={['/runs/r1']}>
      <App />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
});

const PRESET_PLAN: RunDetail['plan'] = {
  mode: 'preset',
  preset: { id: 'mni-mask-analysis', version: '1.1.0', sha256: 'a'.repeat(64) },
  targets: { required: ['tract-disconnection.tract_disconnection'], if_available: ['cognet-profile.profile'] },
};

describe('run page: plan', () => {
  it('names the preset with its version', async () => {
    mockApi({ run: makeRun({ plan: PRESET_PLAN }) });
    renderRun();
    const plan = await screen.findByTestId('run-plan');
    expect(plan).toHaveTextContent('Preset mni-mask-analysis 1.1.0');
    expect(plan.querySelector('[data-plan-mode="preset"]')).not.toBeNull();
  });

  it('lists the targets of a custom plan, required and if available', async () => {
    mockApi({
      run: makeRun({
        plan: {
          mode: 'free',
          preset: null,
          targets: {
            required: ['functional-disconnection.fdisconnection_map'],
            if_available: ['cognet-profile.profile'],
          },
        },
      }),
    });
    renderRun();
    const plan = await screen.findByTestId('run-plan');
    expect(plan).toHaveTextContent('Custom plan');
    expect(plan).toHaveTextContent('Required: functional-disconnection.fdisconnection_map');
    expect(plan).toHaveTextContent('If available: cognet-profile.profile');
  });

  it('says full plan for a full run and for a record without a plan', async () => {
    mockApi({ run: makeRun() });
    const { unmount } = renderRun();
    expect(await screen.findByTestId('run-plan')).toHaveTextContent('Full plan');
    unmount();
    mockApi({ run: makeRun({ plan: undefined }) });
    renderRun();
    expect(await screen.findByTestId('run-plan')).toHaveTextContent('Full plan');
  });

  it('collapses the slots that are not part of the plan and marks the role of the others', async () => {
    mockApi({
      run: makeRun({
        plan: PRESET_PLAN,
        slots: [
          { id: 's1', slot: 'lesion-intake', position: 0, optional: false, status: 'selected', implementation_id: 'lesion-qc', step_name: 'lesion-qc', step_version: '1.0.0', chosen_by: 'preset', plan_role: 'upstream' },
          { id: 's2', slot: 'normalisation', position: 1, optional: false, status: 'skipped', reason: 'condition_false' },
          { id: 's3', slot: 'functional-disconnection', position: 2, optional: true, status: 'skipped', reason: 'not_requested' },
          { id: 's4', slot: 'tract-disconnection', position: 3, optional: true, status: 'selected', implementation_id: 'lnm-tracts', step_name: 'lnm-tracts', step_version: '1.0.0', chosen_by: 'deployment_default', plan_role: 'target' },
          { id: 's5', slot: 'cognitive-profile', position: 4, optional: true, status: 'skipped', reason: 'not_requested' },
        ],
        steps: [],
      }),
    });
    renderRun();
    const timeline = await screen.findByRole('list', { name: 'Step timeline' });
    const collapsed = within(timeline).getByTestId('not-requested');
    expect(within(collapsed).getByText('Not part of this plan (2)')).toBeInTheDocument();
    const details = collapsed.querySelector('details') as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.querySelector('[data-slot="functional-disconnection"]')).not.toBeNull();
    expect(details.querySelector('[data-slot="cognitive-profile"]')).not.toBeNull();
    // planned and not-applicable slots stay in the main list, planned ones with their role
    const intake = timeline.querySelector('li.timeline-item[data-slot="lesion-intake"]') as HTMLElement;
    expect(intake.closest('details')).toBeNull();
    expect(intake).toHaveTextContent('Added by the plan');
    expect(intake).toHaveTextContent('set by the analysis');
    const tract = timeline.querySelector('li.timeline-item[data-slot="tract-disconnection"]') as HTMLElement;
    expect(tract).toHaveTextContent('Requested');
    expect(tract.closest('details')).toBeNull();
    expect(timeline.querySelector('li.timeline-item[data-slot="normalisation"]')?.closest('details')).toBeNull();
  });

  it('has no collapsed section when every slot is in the plan', async () => {
    mockApi({ run: makeRun() });
    renderRun();
    await screen.findByRole('list', { name: 'Step timeline' });
    expect(screen.queryByTestId('not-requested')).not.toBeInTheDocument();
  });

  it('shows the external tool of a slot and the declared origin of a supplied input', async () => {
    mockApi({
      run: makeRun({
        slots: [
          { id: 's1', slot: 'segmentation', position: 0, optional: false, status: 'selected', implementation_id: 'ext-seg', step_name: 'ext-seg', step_version: '1.0.0', chosen_by: 'user', plan_role: 'target', upstream: { name: 'example-segmenter', version: '2.1.0', licence: 'Apache-2.0', homepage: 'https://example.org/x' } },
          { id: 's2', slot: 'lesion-intake', position: 1, optional: false, status: 'selected', implementation_id: 'lesion-qc', step_name: 'lesion-qc', step_version: '1.0.0', chosen_by: 'deployment_default', plan_role: 'upstream', upstream: null },
        ],
        steps: [],
        inputs: [
          { role: 'lesion_native', input_file_id: 'f1', origin: { kind: 'external-tool', tool: 'other-tool', version: '1.2' } },
          { role: 'ct', input_file_id: 'f2', origin: { kind: 'manual' } },
          { role: 'lesion_mni', input_file_id: 'f3', origin: null },
        ],
      }),
    });
    renderRun();
    const timeline = await screen.findByRole('list', { name: 'Step timeline' });
    const segmentation = timeline.querySelector('li[data-slot="segmentation"]') as HTMLElement;
    expect(within(segmentation).getByTestId('upstream')).toHaveTextContent(
      'External tool: example-segmenter 2.1.0 (Apache-2.0)',
    );
    expect(within(segmentation).getByText('External tool')).toBeInTheDocument();
    const intake = timeline.querySelector('li[data-slot="lesion-intake"]') as HTMLElement;
    expect(within(intake).queryByTestId('upstream')).not.toBeInTheDocument();
    expect(screen.getByTestId('origin-lesion_native')).toHaveTextContent(
      'Declared by the user (not verified): external tool other-tool 1.2',
    );
    expect(screen.getByTestId('origin-ct')).toHaveTextContent('Declared by the user (not verified): drawn by hand');
    expect(screen.queryByTestId('origin-lesion_mni')).not.toBeInTheDocument();
  });

  it('opens the wizard with the recorded mode when running again with new files', async () => {
    mockApi({ run: makeRun({ plan: PRESET_PLAN, input_set: 'mni-mask' }) });
    renderRun();
    await userEvent.click(await screen.findByRole('button', { name: 'Run again with new files' }));
    expect(await screen.findByRole('heading', { name: 'New case' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Lesion mask already in MNI/ })).toBeChecked();
  });

  it('runs again on the same case: no files step, nothing uploaded again', async () => {
    mockApi({
      run: makeRun({ status: 'succeeded', plan: PRESET_PLAN, input_set: 'mni-mask', inputs: [{ role: 'lesion_mni' }] }),
    });
    renderRun();
    await userEvent.click(await screen.findByRole('button', { name: 'Run again with the same plan' }));
    expect(await screen.findByRole('heading', { name: 'New run on this case' })).toBeInTheDocument();
    expect(screen.getByTestId('same-case-note')).toHaveTextContent('nothing is uploaded again');
    const steps = within(screen.getByRole('list', { name: 'Wizard progress' })).getAllByRole('listitem');
    expect(steps.map((item) => item.textContent)).toEqual(['1. Analysis', '2. Implementations']);
    expect(screen.queryByLabelText('Lesion mask in MNI space (.nii.gz)')).not.toBeInTheDocument();
  });

  it('cannot run again on the same case while the run is active', async () => {
    mockApi({ run: makeRun({ status: 'running', inputs: [{ role: 'lesion_mni' }] }) });
    renderRun();
    expect(await screen.findByRole('button', { name: 'Run again with the same plan' })).toBeDisabled();
    expect(screen.getByText(/A run of this case is active/)).toBeInTheDocument();
    // a new case with new files is always possible
    expect(screen.getByRole('button', { name: 'Run again with new files' })).toBeEnabled();
  });

  it('offers no same-case run again when the run detail lists no inputs', async () => {
    mockApi({ run: makeRun({ status: 'succeeded', inputs: [] }) });
    renderRun();
    await screen.findByRole('button', { name: 'Run again with new files' });
    expect(screen.queryByRole('button', { name: 'Run again with the same plan' })).not.toBeInTheDocument();
  });
});
