import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../../App';
import type { PlanSlot } from '../../api/v1';
import i18n from '../../i18n';
import { DEVELOPMENT, makePreset, makeRun, makeSlots, mockApi } from '../../test/casesMock';
import { TOKEN_KEY } from '../auth/AuthContext';
import { AnalysisStep } from './AnalysisStep';
import { DevelopmentList } from './DevelopmentList';
import { CUSTOM_KEY, buildAnalysisOptions } from './plan';
import { SlotSelector } from './SlotSelector';
import { developmentBlocked, developmentSlotNames, implementationTitle } from './slots';

const MNI = ['lesion_mni'];
const CT = ['ct', 'lesion_native'];

function renderAt(path: string) {
  window.localStorage.setItem(TOKEN_KEY, 'jwt-123');
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

const next = () => userEvent.click(screen.getByRole('button', { name: 'Next' }));
const mask = () => new File(['x'], 'original-name.nii.gz', { type: 'application/gzip' });

beforeEach(() => {
  window.localStorage.clear();
});

describe('DevelopmentList', () => {
  it('lists every stage greyed, labelled "In development", with no control that could select it', () => {
    render(<DevelopmentList stages={DEVELOPMENT} hint="Not available yet." />);
    const section = screen.getByTestId('in-development');
    expect(within(section).getByRole('heading', { name: 'In development' })).toBeInTheDocument();
    const items = within(section).getAllByRole('listitem');
    expect(items.map((item) => item.getAttribute('data-development'))).toEqual([
      'normalisation',
      'segmentation',
      'lesion-bank',
      'slnm-coarse',
      'voxel-lnm',
    ]);
    for (const item of items) {
      expect(item).toHaveAttribute('aria-disabled', 'true');
      expect(within(item).getByText('In development')).toBeInTheDocument();
    }
    expect(within(section).getByText('Lesion segmentation')).toBeInTheDocument();
    expect(within(section).queryByRole('radio')).not.toBeInTheDocument();
    expect(within(section).queryByRole('checkbox')).not.toBeInTheDocument();
    expect(within(section).queryByRole('combobox')).not.toBeInTheDocument();
    expect(within(section).queryByRole('button')).not.toBeInTheDocument();
  });

  it('is translated by stage id and falls back to the API text for an unknown stage', async () => {
    await i18n.changeLanguage('es');
    render(
      <DevelopmentList
        stages={[DEVELOPMENT[1], { id: 'future-stage', title: 'Future stage', description: 'Text from the API.' }]}
      />,
    );
    expect(screen.getByRole('heading', { name: 'En desarrollo' })).toBeInTheDocument();
    expect(screen.getByText('Segmentación de la lesión')).toBeInTheDocument();
    expect(screen.getByText('Future stage')).toBeInTheDocument();
    expect(screen.getByText('Text from the API.')).toBeInTheDocument();
    expect(screen.getAllByText('En desarrollo').length).toBeGreaterThanOrEqual(3);
  });

  it('renders nothing when nothing is in development', () => {
    render(<DevelopmentList stages={[]} />);
    expect(screen.queryByTestId('in-development')).not.toBeInTheDocument();
  });
});

describe('in-development helpers', () => {
  it('names the slots that are in development and blocks only the modes that need them', () => {
    const slots = makeSlots();
    expect([...developmentSlotNames(DEVELOPMENT)]).toEqual(['normalisation']);
    expect(developmentBlocked(slots, DEVELOPMENT, CT)).toBe(true);
    expect(developmentBlocked(slots, DEVELOPMENT, MNI)).toBe(false);
    expect(developmentBlocked(slots, [], CT)).toBe(false);
    // Stages without a slot never block anything.
    expect(developmentBlocked(slots, DEVELOPMENT.slice(1), CT)).toBe(false);
  });
});

describe('implementation titles', () => {
  const impl = (id: string, title?: string) => ({
    id,
    title,
    step_name: 'flnm-fast',
    step_version: '3.0.0',
    available: true,
    enabled: true,
    is_default: false,
    is_pipeline_default: false,
  });

  it('tells the two HCP cohort entries of one step image apart, in English and Spanish', async () => {
    expect(implementationTitle(i18n.t, impl('flnm-fast-hcp-aging', 'flnm-fast'))).toBe('flnm-fast · HCP Aging');
    expect(implementationTitle(i18n.t, impl('flnm-fast-hcp-ya', 'flnm-fast'))).toBe('flnm-fast · HCP Young Adult');
    await i18n.changeLanguage('es');
    expect(implementationTitle(i18n.t, impl('flnm-fast-hcp-aging', 'flnm-fast'))).toBe('flnm-fast · HCP Envejecimiento');
    expect(implementationTitle(i18n.t, impl('flnm-fast-hcp-ya', 'flnm-fast'))).toBe('flnm-fast · HCP Adultos jóvenes');
  });

  it('uses the API title, then the step name and version, for an id the catalogue does not know', () => {
    expect(implementationTitle(i18n.t, impl('other', 'Other title'))).toBe('Other title');
    expect(implementationTitle(i18n.t, impl('other'))).toBe('flnm-fast 3.0.0');
  });
});

describe('SlotSelector with cohort titles and stages in development', () => {
  it('shows the cohort title of each candidate in the options', () => {
    const slots = makeSlots();
    const base = slots[2].implementations[0];
    slots[2].implementations = [
      { ...base, id: 'flnm-fast-hcp-aging', title: 'flnm-fast · HCP Aging', is_default: true, stub: false },
      { ...base, id: 'flnm-fast-hcp-ya', title: 'flnm-fast · HCP Young Adult', is_default: false, stub: false },
    ];
    slots[2].default_implementation = 'flnm-fast-hcp-aging';
    render(<SlotSelector slots={slots} roles={MNI} value={{}} onChange={() => {}} />);
    expect(screen.getByRole('option', { name: /flnm-fast · HCP Aging/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'flnm-fast · HCP Young Adult' })).toBeInTheDocument();
  });

  it('greys a slot in development: a label, no selector, nothing to choose', () => {
    const slots = makeSlots();
    const plan: PlanSlot[] = [
      { name: 'lesion-intake', status: 'selected', plan_role: 'target', implementation: 'lesion-qc' },
      { name: 'normalisation', status: 'unavailable', reason: 'no_implementation', plan_role: 'target' },
    ];
    render(
      <SlotSelector slots={slots} roles={CT} value={{}} onChange={() => {}} plan={plan} development={DEVELOPMENT} />,
    );
    const row = document.querySelector('[data-slot="normalisation"]') as HTMLElement;
    expect(row).toHaveAttribute('data-state', 'development');
    expect(within(row).getByText('In development')).toBeInTheDocument();
    expect(within(row).queryByRole('combobox')).not.toBeInTheDocument();
    expect(within(row).queryByText('Unavailable')).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Lesion intake and QC' })).toBeInTheDocument();
  });
});

describe('AnalysisStep with stages in development', () => {
  it('disables the custom output of a slot in development and labels it in the plan preview', () => {
    render(
      <AnalysisStep
        options={buildAnalysisOptions([], 'ct-and-native-mask', true)}
        selected={CUSTOM_KEY}
        onSelect={() => {}}
        slots={makeSlots()}
        roles={CT}
        custom={{}}
        onCustomChange={() => {}}
        preview={{
          status: 'ready',
          error: '',
          data: {
            feasible: true,
            mode: 'free',
            input_set: 'ct-and-native-mask',
            slots: [{ name: 'normalisation', status: 'unavailable', reason: 'no_implementation', plan_role: 'target' }],
            reasons: [],
          },
        }}
        development={DEVELOPMENT}
      />,
    );
    const row = document.querySelector('.slot-row[data-slot="normalisation"]') as HTMLElement;
    expect(row).toHaveAttribute('data-state', 'development');
    expect(within(row).getByRole('combobox')).toBeDisabled();
    expect(within(row).getByText('In development')).toBeInTheDocument();
    expect(within(row).queryByText('Unavailable')).not.toBeInTheDocument();
    const planRow = screen.getByRole('region', { name: 'Plan' }).querySelector('[data-slot="normalisation"]') as HTMLElement;
    expect(planRow).toHaveAttribute('data-development', 'true');
    expect(planRow).toHaveTextContent('In development');
    expect(planRow).not.toHaveTextContent('Unavailable');
    // The functional disconnection output can still be requested.
    expect(screen.getByRole('combobox', { name: 'Functional disconnection: request' })).toBeEnabled();
  });
});

describe('case wizard with stages in development', () => {
  it('greys the CT mode, starts in the MNI mode and lists the stages on the analysis and steps pages', async () => {
    mockApi({
      development: DEVELOPMENT,
      presets: [makePreset('mni', { title: 'MNI analysis', input_sets: ['mni-mask'], is_default: true })],
    });
    renderAt('/cases/new');
    await screen.findByRole('heading', { name: 'New case' });

    const ct = screen.getByRole('radio', { name: /CT and lesion mask/ });
    await waitFor(() => expect(screen.getByRole('radio', { name: /Lesion mask already in MNI/ })).toBeChecked());
    expect(ct).toBeDisabled();
    expect(ct).not.toBeChecked();
    expect(ct.closest('label')).toHaveAttribute('data-development', 'true');
    expect(within(ct.closest('label') as HTMLElement).getByText('In development')).toBeInTheDocument();
    await next();

    await userEvent.upload(screen.getByLabelText('Lesion mask in MNI space (.nii.gz)'), mask());
    await next();
    await screen.findByRole('group', { name: 'Analysis' });
    const list = await screen.findByTestId('in-development');
    expect(within(list).getAllByRole('listitem')).toHaveLength(4);
    // Only the analysis is a radio; the stages in development add none.
    expect(screen.getAllByRole('radio')).toHaveLength(1);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled());
    await next();
    expect(await screen.findByTestId('in-development')).toBeInTheDocument();
    expect(screen.getByText('Lesion bank')).toBeInTheDocument();
  });

  it('leaves the CT mode selectable when nothing is in development', async () => {
    mockApi({ development: [] });
    renderAt('/cases/new');
    await screen.findByRole('heading', { name: 'New case' });
    expect(screen.getByRole('radio', { name: /CT and lesion mask/ })).toBeEnabled();
    expect(screen.getByRole('radio', { name: /CT and lesion mask/ })).toBeChecked();
  });

  it('ignores a failing in-development call (older server)', async () => {
    mockApi({
      routes: {
        'GET /v1/pipelines/stroke-lnm/versions/0.1.0/in-development': () => new Response('{}', { status: 404 }),
      },
    });
    renderAt('/cases/new');
    await screen.findByRole('heading', { name: 'New case' });
    expect(screen.getByRole('radio', { name: /CT and lesion mask/ })).toBeEnabled();
    expect(screen.queryByTestId('in-development')).not.toBeInTheDocument();
  });
});

describe('run page with stages in development', () => {
  it('lists them under the timeline as not part of the run and names the cohort of the implementation', async () => {
    const run = makeRun({
      slots: [
        {
          id: 's1',
          slot: 'functional-disconnection',
          position: 0,
          optional: true,
          status: 'selected',
          implementation_id: 'flnm-fast-hcp-aging',
          step_name: 'flnm-fast',
          step_version: '3.0.0',
          chosen_by: 'deployment_default',
          stub: false,
        },
      ],
      steps: [],
    });
    mockApi({ run, development: DEVELOPMENT });
    renderAt('/runs/r1');
    const timeline = await screen.findByRole('list', { name: 'Step timeline' });
    expect(within(timeline).getByText('flnm-fast · HCP Aging')).toBeInTheDocument();
    const section = await screen.findByTestId('in-development');
    expect(within(section).getByRole('heading', { name: 'Not part of this run' })).toBeInTheDocument();
    expect(within(section).getAllByRole('listitem')).toHaveLength(4);
    expect(within(section).queryByRole('button')).not.toBeInTheDocument();
  });

  it('shows a synthetic example input as provided by the platform, not as declared by the user', async () => {
    const run = makeRun({
      inputs: [
        {
          role: 'lesion_mni',
          origin: { kind: 'external-tool', tool: 'stroke-server-example', version: 'mni-lesion-left-medium' },
        },
      ],
    });
    mockApi({ run });
    renderAt('/runs/r1');
    const origin = await screen.findByTestId('origin-lesion_mni');
    expect(origin).toHaveTextContent('Provided by the platform: Synthetic example lesion');
    expect(origin).not.toHaveTextContent('Declared by the user');
    expect(origin).not.toHaveTextContent('external tool');
  });
});
