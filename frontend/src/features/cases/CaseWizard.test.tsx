import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '../../App';
import { jsonResponse, makeRun, makeSlots, mockApi, type Call } from '../../test/casesMock';
import { TOKEN_KEY } from '../auth/AuthContext';

function renderWizard() {
  window.localStorage.setItem(TOKEN_KEY, 'jwt-123');
  return render(
    <MemoryRouter initialEntries={['/cases/new']}>
      <App />
    </MemoryRouter>,
  );
}

const next = () => userEvent.click(screen.getByRole('button', { name: 'Next' }));
const calls = (all: Call[], method: string, path: string) => all.filter((c) => c.method === method && c.path === path);
const mask = () => new File(['x'], 'original-name.nii.gz', { type: 'application/gzip' });

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  window.localStorage.clear();
});

describe('case wizard', () => {
  it('mode MNI: uploads one mask, sends band and sex, starts the run and opens the run page', async () => {
    const { calls: all } = mockApi({ run: makeRun({ input_set: 'mni-mask' }) });
    renderWizard();
    await screen.findByRole('heading', { name: 'New case' });

    await userEvent.click(screen.getByRole('radio', { name: /Lesion mask already in MNI/ }));
    await userEvent.selectOptions(screen.getByLabelText('Sex (optional)'), 'female');
    await userEvent.selectOptions(screen.getByLabelText('Age band (optional)'), '60-64');
    await next();

    expect(screen.getByLabelText('Lesion mask in MNI space (.nii.gz)')).toBeInTheDocument();
    expect(screen.queryByLabelText('CT scan (.nii.gz)')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    await userEvent.upload(screen.getByLabelText('Lesion mask in MNI space (.nii.gz)'), mask());
    await next();

    await screen.findByRole('combobox', { name: 'Functional disconnection' });
    expect(screen.getByRole('combobox', { name: 'Functional disconnection' })).toHaveValue('fdc-stub');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Functional disconnection' }), 'fdc-alt');
    await userEvent.click(screen.getByRole('button', { name: 'Create case and start run' }));

    expect(await screen.findByRole('heading', { name: 'Run' })).toBeInTheDocument();
    const created = calls(all, 'POST', '/v1/cases');
    expect(created).toHaveLength(1);
    expect(JSON.parse(created[0].body as string)).toEqual({ sex: 'female', age_band: '60-64' });
    const uploads = all.filter((c) => c.method === 'PUT');
    expect(uploads.map((u) => u.path)).toEqual(['/v1/cases/c1/inputs/lesion_mni']);
    const form = uploads[0].body as FormData;
    expect([...form.keys()]).toEqual(['file']);
    const run = JSON.parse(calls(all, 'POST', '/v1/cases/c1/runs')[0].body as string);
    expect(run).toEqual({
      pipeline_id: 'stroke-lnm',
      pipeline_version: '0.1.0',
      input_roles: ['lesion_mni'],
      implementations: { 'functional-disconnection': 'fdc-alt' },
    });
  });

  it('mode CT: uploads CT and native mask, sends no overrides when defaults are kept', async () => {
    const { calls: all } = mockApi({
      slots: makeSlots({ normalisationMounted: true }),
      run: makeRun({ input_set: 'ct-and-native-mask' }),
    });
    renderWizard();
    await screen.findByRole('heading', { name: 'New case' });
    await next();

    await userEvent.upload(screen.getByLabelText('CT scan (.nii.gz)'), mask());
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    await userEvent.upload(screen.getByLabelText('Lesion mask on the CT (.nii.gz)'), mask());
    await next();

    await screen.findByRole('combobox', { name: 'CT lesion to MNI152' });
    await userEvent.click(screen.getByRole('button', { name: 'Create case and start run' }));
    await screen.findByRole('heading', { name: 'Run' });

    expect(JSON.parse(calls(all, 'POST', '/v1/cases')[0].body as string)).toEqual({ sex: null, age_band: null });
    expect(all.filter((c) => c.method === 'PUT').map((u) => u.path)).toEqual([
      '/v1/cases/c1/inputs/ct',
      '/v1/cases/c1/inputs/lesion_native',
    ]);
    expect(JSON.parse(calls(all, 'POST', '/v1/cases/c1/runs')[0].body as string)).toMatchObject({
      input_roles: ['ct', 'lesion_native'],
      implementations: {},
    });
  });

  it('mode CT: cannot start when the normalisation step is not mounted in the deployment', async () => {
    const { calls: all } = mockApi();
    renderWizard();
    await screen.findByRole('heading', { name: 'New case' });
    await next();
    await userEvent.upload(screen.getByLabelText('CT scan (.nii.gz)'), mask());
    await userEvent.upload(screen.getByLabelText('Lesion mask on the CT (.nii.gz)'), mask());
    await next();

    expect(await screen.findByText(/required step is unavailable: CT lesion to MNI152/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create case and start run' })).toBeDisabled();
    expect(calls(all, 'POST', '/v1/cases')).toHaveLength(0);
  });

  it('does not offer an exact age field and never sends one', async () => {
    mockApi();
    renderWizard();
    await screen.findByRole('heading', { name: 'New case' });
    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('reuses the created case when starting the run fails and is retried', async () => {
    let runAttempts = 0;
    const { calls: all } = mockApi({
      run: makeRun(),
      routes: {
        'POST /v1/cases/c1/runs': () => {
          runAttempts += 1;
          return runAttempts === 1
            ? jsonResponse({ detail: 'A run is already active', code: 'active_run_exists' }, 409)
            : jsonResponse(makeRun(), 201);
        },
      },
    });
    renderWizard();
    await screen.findByRole('heading', { name: 'New case' });
    await userEvent.click(screen.getByRole('radio', { name: /Lesion mask already in MNI/ }));
    await next();
    await userEvent.upload(screen.getByLabelText('Lesion mask in MNI space (.nii.gz)'), mask());
    await next();
    await screen.findByRole('combobox', { name: 'Functional disconnection' });
    const start = screen.getByRole('button', { name: 'Create case and start run' });
    await userEvent.click(start);
    expect(await screen.findByText('A run is already active')).toBeInTheDocument();
    await waitFor(() => expect(start).toBeEnabled());
    await userEvent.click(start);
    await screen.findByRole('heading', { name: 'Run' });
    expect(calls(all, 'POST', '/v1/cases')).toHaveLength(1);
    expect(all.filter((c) => c.method === 'PUT')).toHaveLength(1);
  });
});
