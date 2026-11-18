import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { PlanSlot } from '../../api/v1';
import { makeSlots } from '../../test/casesMock';
import { SlotSelector } from './SlotSelector';
import { blockingSlots, defaultChoices, overrides } from './slots';

const MNI = ['lesion_mni'];
const CT = ['ct', 'lesion_native'];

describe('SlotSelector', () => {
  it('preselects the deployment default of each slot', () => {
    const slots = makeSlots({ normalisationMounted: true });
    render(<SlotSelector slots={slots} roles={CT} value={defaultChoices(slots)} onChange={() => {}} />);
    expect(screen.getByRole('combobox', { name: 'Lesion intake and QC' })).toHaveValue('lesion-qc');
    expect(screen.getByRole('combobox', { name: 'CT lesion to MNI152' })).toHaveValue('ct-lesion-to-mni152');
    expect(screen.getByRole('combobox', { name: 'Functional disconnection' })).toHaveValue('fdc-stub');
    expect(screen.getByRole('option', { name: 'fdc-stub 1.0.0 (deployment default, stub)' })).toBeInTheDocument();
  });

  it('labels a reference-tested implementation as experimental', () => {
    const slots = makeSlots();
    slots[2].implementations[1] = { ...slots[2].implementations[1], validation_level: 'reference-tested', experimental: true };
    render(<SlotSelector slots={slots} roles={MNI} value={defaultChoices(slots)} onChange={() => {}} />);
    expect(screen.getByRole('option', { name: 'Alternative connectome (experimental)' })).toBeInTheDocument();
  });

  it('shows the external tool and the validation level of each candidate', () => {
    const slots = makeSlots();
    slots[2].implementations[0] = { ...slots[2].implementations[0], validation_level: 'validated', experimental: false };
    slots[2].implementations[1] = {
      ...slots[2].implementations[1],
      validation_level: 'reference-tested',
      experimental: true,
      upstream: { name: 'ext-tool', version: '2.3.4', licence: 'MIT' },
    };
    render(<SlotSelector slots={slots} roles={MNI} value={defaultChoices(slots)} onChange={() => {}} />);
    const row = screen.getByRole('combobox', { name: 'Functional disconnection' }).closest('li') as HTMLElement;
    const list = within(row).getByRole('list', { name: 'Candidate implementations' });
    const stub = list.querySelector('[data-impl="fdc-stub"]') as HTMLElement;
    expect(within(stub).getByTestId('impl-level')).toHaveTextContent('validated');
    expect(within(stub).queryByTestId('impl-external')).not.toBeInTheDocument();
    expect(stub).toHaveAttribute('data-current', 'true');
    const alt = list.querySelector('[data-impl="fdc-alt"]') as HTMLElement;
    expect(within(alt).getByTestId('impl-external')).toHaveTextContent('External tool: ext-tool 2.3.4 (MIT)');
    expect(within(alt).getByTestId('impl-level')).toHaveTextContent('reference-tested - experimental');
    expect(alt).not.toHaveAttribute('data-current');
  });

  it('marks the picked candidate as the current one', () => {
    const slots = makeSlots();
    slots[2].implementations[1] = { ...slots[2].implementations[1], validation_level: 'unvalidated' };
    render(<SlotSelector slots={slots} roles={MNI} value={{ 'functional-disconnection': 'fdc-alt' }} onChange={() => {}} />);
    const row = screen.getByRole('combobox', { name: 'Functional disconnection' }).closest('li') as HTMLElement;
    expect(row.querySelector('[data-impl="fdc-alt"]')).toHaveAttribute('data-current', 'true');
    expect(within(row).getByTestId('impl-level')).toHaveTextContent('unvalidated');
  });

  it('lists nothing for candidates that carry no level and no external tool (older server)', () => {
    const slots = makeSlots();
    render(<SlotSelector slots={slots} roles={MNI} value={defaultChoices(slots)} onChange={() => {}} />);
    expect(screen.queryByRole('list', { name: 'Candidate implementations' })).not.toBeInTheDocument();
  });

  it('shows the badges of the preset-fixed implementation only', () => {
    const slots = makeSlots();
    slots[2].implementations[0] = { ...slots[2].implementations[0], upstream: { name: 'other', version: '9' } };
    slots[2].implementations[1] = { ...slots[2].implementations[1], upstream: { name: 'ext-tool', version: '1' } };
    const plan: PlanSlot[] = [
      { name: 'functional-disconnection', status: 'selected', plan_role: 'target', implementation: 'fdc-alt' },
    ];
    render(
      <SlotSelector slots={slots} roles={MNI} value={{}} plan={plan} fixed={{ 'functional-disconnection': 'fdc-alt' }} onChange={() => {}} />,
    );
    const externals = screen.getAllByTestId('impl-external');
    expect(externals).toHaveLength(1);
    expect(externals[0]).toHaveTextContent('ext-tool 1');
  });

  it('reports the implementation the user picks', async () => {
    const slots = makeSlots();
    const onChange = vi.fn();
    render(<SlotSelector slots={slots} roles={MNI} value={defaultChoices(slots)} onChange={onChange} />);
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Functional disconnection' }), 'fdc-alt');
    expect(onChange).toHaveBeenCalledWith('functional-disconnection', 'fdc-alt');
  });

  it('marks a slot without an available implementation and offers no selector for it', () => {
    const slots = makeSlots();
    render(<SlotSelector slots={slots} roles={CT} value={defaultChoices(slots)} onChange={() => {}} />);
    const row = screen.getByText('CT lesion to MNI152').closest('li') as HTMLElement;
    expect(row).toHaveAttribute('data-state', 'unavailable');
    expect(within(row).getByText('Unavailable')).toBeInTheDocument();
    expect(within(row).queryByRole('combobox')).not.toBeInTheDocument();
    expect(within(row).getByText(/cannot start/)).toBeInTheDocument();
  });

  it('shows a slot whose condition is not met as not used in this mode', () => {
    const slots = makeSlots();
    render(<SlotSelector slots={slots} roles={MNI} value={defaultChoices(slots)} onChange={() => {}} />);
    const row = screen.getByText('CT lesion to MNI152').closest('li') as HTMLElement;
    expect(row).toHaveAttribute('data-state', 'unused');
    expect(within(row).getByText('Not used in this mode')).toBeInTheDocument();
  });

  it('disables an implementation that is not available', () => {
    const slots = makeSlots();
    slots[2].implementations[1] = { ...slots[2].implementations[1], available: false };
    render(<SlotSelector slots={slots} roles={MNI} value={defaultChoices(slots)} onChange={() => {}} />);
    expect(screen.getByRole('option', { name: /Alternative connectome - Unavailable/ })).toBeDisabled();
  });
});

describe('slot helpers', () => {
  it('sends only the choices that differ from the default', () => {
    const slots = makeSlots();
    const choices = defaultChoices(slots);
    expect(overrides(slots, choices)).toEqual({});
    expect(overrides(slots, { ...choices, 'functional-disconnection': 'fdc-alt' })).toEqual({
      'functional-disconnection': 'fdc-alt',
    });
  });

  it('blocks only a required, applicable, unavailable slot', () => {
    const slots = makeSlots();
    expect(blockingSlots(slots, CT).map((s) => s.name)).toEqual(['normalisation']);
    expect(blockingSlots(slots, MNI)).toEqual([]);
    expect(blockingSlots(makeSlots({ normalisationMounted: true }), CT)).toEqual([]);
  });
});

describe('SlotSelector within a plan', () => {
  const plan: PlanSlot[] = [
    { name: 'lesion-intake', status: 'selected', plan_role: 'upstream', implementation: 'lesion-qc' },
    { name: 'normalisation', status: 'skipped', reason: 'not_requested' },
    { name: 'functional-disconnection', status: 'selected', plan_role: 'if_available', implementation: 'fdc-stub' },
  ];

  it('lists only the planned slots, with their role, and not the slots outside the plan', () => {
    const slots = makeSlots();
    render(<SlotSelector slots={slots} roles={MNI} value={defaultChoices(slots)} plan={plan} onChange={() => {}} />);
    expect(screen.queryByText('CT lesion to MNI152')).not.toBeInTheDocument();
    const intake = screen.getByRole('combobox', { name: 'Lesion intake and QC' }).closest('li') as HTMLElement;
    expect(within(intake).getByText('Added by the plan')).toBeInTheDocument();
    const functional = screen.getByRole('combobox', { name: 'Functional disconnection' }).closest('li') as HTMLElement;
    expect(within(functional).getByText('If available')).toBeInTheDocument();
  });

  it('shows a choice fixed by the preset read-only', async () => {
    const slots = makeSlots();
    const onChange = vi.fn();
    render(
      <SlotSelector
        slots={slots}
        roles={MNI}
        value={{ 'functional-disconnection': 'fdc-alt' }}
        plan={plan}
        fixed={{ 'functional-disconnection': 'fdc-alt' }}
        onChange={onChange}
      />,
    );
    expect(screen.queryByRole('combobox', { name: 'Functional disconnection' })).not.toBeInTheDocument();
    const row = screen.getByText('Functional disconnection').closest('li') as HTMLElement;
    expect(within(row).getByText(/Fixed by this analysis/)).toHaveTextContent('Alternative connectome');
    expect(onChange).not.toHaveBeenCalled();
  });

  it('keeps marking a planned slot without an implementation as unavailable', () => {
    const slots = makeSlots();
    const withNormalisation: PlanSlot[] = [
      ...plan.filter((s) => s.name !== 'normalisation'),
      { name: 'normalisation', status: 'unavailable', reason: 'no_implementation', plan_role: 'upstream' },
    ];
    render(<SlotSelector slots={slots} roles={CT} value={{}} plan={withNormalisation} onChange={() => {}} />);
    const row = screen.getByText('CT lesion to MNI152').closest('li') as HTMLElement;
    expect(row).toHaveAttribute('data-state', 'unavailable');
    expect(within(row).queryByRole('combobox')).not.toBeInTheDocument();
  });
});
