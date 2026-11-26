import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import i18n from '../../i18n';
import { makePreset, makeSlots } from '../../test/casesMock';
import { AnalysisStep } from './AnalysisStep';
import { IDLE_PREVIEW } from './PlanPreview';
import { buildAnalysisOptions } from './plan';
import { SlotSelector } from './SlotSelector';
import { implementationLabel, presetDescription, presetTitle, slotLabel } from './slots';

const t = i18n.t.bind(i18n) as unknown as Parameters<typeof presetTitle>[0];
const MNI = ['lesion_mni'];

const PRESET = makePreset('mni-mask-analysis', {
  title: 'Disconnection analysis of an MNI lesion mask',
  description: 'English text from the preset file.',
  input_sets: ['mni-mask'],
  is_default: true,
});

describe('preset titles and descriptions', () => {
  it('come from the catalogue by preset id, else from the preset file', async () => {
    expect(presetTitle(t, PRESET)).toBe('Disconnection analysis of an MNI lesion mask');
    await i18n.changeLanguage('es');
    expect(presetTitle(t, PRESET)).toBe('Análisis de desconexión de una máscara de lesión en MNI');
    expect(presetDescription(t, PRESET)).toMatch(/^Control de calidad de la lesión, desconexión funcional/);
    const unknown = makePreset('a-new-preset', { title: 'A new preset', description: 'Its file text.' });
    expect(presetTitle(t, unknown)).toBe('A new preset');
    expect(presetDescription(t, unknown)).toBe('Its file text.');
    expect(presetDescription(t, makePreset('full', { description: null }))).toBe('');
  });

  it('are shown in Spanish in the analysis step', async () => {
    await i18n.changeLanguage('es');
    render(
      <AnalysisStep
        options={buildAnalysisOptions([PRESET], 'mni-mask', false)}
        selected={null}
        onSelect={() => {}}
        slots={makeSlots()}
        roles={MNI}
        custom={{}}
        onCustomChange={() => {}}
        preview={IDLE_PREVIEW}
      />,
    );
    const radio = screen.getByRole('radio', { name: /Análisis de desconexión de una máscara de lesión en MNI/ });
    const label = radio.closest('label') as HTMLElement;
    expect(within(label).getByText(/para una máscara de lesión que ya está en espacio MNI/)).toBeInTheDocument();
    expect(screen.queryByText(/Disconnection analysis of an MNI lesion mask/)).not.toBeInTheDocument();
    expect(screen.queryByText('English text from the preset file.')).not.toBeInTheDocument();
  });
});

describe('implementation and slot labels', () => {
  it('tell the English and the Spanish report apart in both languages', async () => {
    const en = implementationLabel(t, 'research-report', 'Research report');
    const enEs = implementationLabel(t, 'research-report-es', 'Research report');
    expect(en).toBe('Research report (English)');
    expect(enEs).toBe('Research report (Spanish)');
    await i18n.changeLanguage('es');
    expect(implementationLabel(t, 'research-report', 'Research report')).toBe('Informe de investigación (inglés)');
    expect(implementationLabel(t, 'research-report-es', 'Research report')).toBe('Informe de investigación (español)');
  });

  it('translate the slot titles, with the API title or the name as fallback', async () => {
    expect(slotLabel(t, 'functional-disconnection', 'API title')).toBe('Functional disconnection');
    expect(slotLabel(t, 'a-new-slot', 'API title')).toBe('API title');
    expect(slotLabel(t, 'a-new-slot')).toBe('a-new-slot');
    await i18n.changeLanguage('es');
    expect(slotLabel(t, 'functional-disconnection', 'Functional disconnection')).toBe('Desconexión funcional');
    expect(implementationLabel(t, 'fdisconnection-stub', 'Functional disconnection (stub)')).toBe('Desconexión funcional (simulada)');
  });

  it('are used by the implementation selector in Spanish', async () => {
    await i18n.changeLanguage('es');
    const slots = makeSlots().map((slot) =>
      slot.name === 'functional-disconnection'
        ? {
            ...slot,
            implementations: [
              { ...slot.implementations[0], id: 'research-report', title: 'Research report', is_default: true },
              { ...slot.implementations[0], id: 'research-report-es', title: 'Research report', is_default: false, stub: false },
            ],
          }
        : slot,
    );
    render(<SlotSelector slots={slots} roles={MNI} value={{}} onChange={() => {}} />);
    const select = screen.getByRole('combobox', { name: 'Desconexión funcional' });
    const options = within(select).getAllByRole('option').map((option) => option.textContent);
    expect(options[0]).toMatch(/^Informe de investigación \(inglés\)/);
    expect(options[1]).toMatch(/^Informe de investigación \(español\)/);
  });
});

describe('validation level help', () => {
  it('explains the levels under the implementation selector and on the level tag', () => {
    const slots = makeSlots().map((slot) =>
      slot.name === 'functional-disconnection'
        ? {
            ...slot,
            implementations: slot.implementations.map((impl) => ({
              ...impl,
              validation_level: 'reference-tested' as const,
              experimental: true,
            })),
          }
        : slot,
    );
    render(<SlotSelector slots={slots} roles={MNI} value={{}} onChange={() => {}} />);
    const help = screen.getByTestId('validation-help');
    expect(help).toHaveTextContent('"reference-tested" means');
    expect(help).toHaveTextContent('"validated" needs a signed validation record');
    expect(help).toHaveTextContent('research use only');
    for (const tag of screen.getAllByTestId('impl-level')) {
      expect(tag).toHaveAttribute('title', help.textContent);
    }
  });

  it('is not shown when no candidate has a validation level', () => {
    render(<SlotSelector slots={makeSlots()} roles={MNI} value={{}} onChange={() => {}} />);
    expect(screen.queryByTestId('validation-help')).not.toBeInTheDocument();
  });
});
