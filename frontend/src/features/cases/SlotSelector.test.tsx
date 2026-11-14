import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
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
