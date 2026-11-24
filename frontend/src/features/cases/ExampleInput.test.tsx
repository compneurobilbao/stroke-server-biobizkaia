import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '../../App';
import { jsonResponse, makeRun, mockApi, type Call } from '../../test/casesMock';
import { TOKEN_KEY } from '../auth/AuthContext';

const EXAMPLE = {
  id: 'ex-left',
  role: 'lesion_mni',
  label: 'Left hemisphere, medium',
  description: 'Synthetic mask.',
  licence: 'CC0-1.0',
  synthetic: true,
};
const EXAMPLE_BUTTON = 'Use the example lesion (synthetic)';

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

async function toFilesStep() {
  await screen.findByRole('heading', { name: 'New case' });
  await userEvent.click(screen.getByRole('radio', { name: /Lesion mask already in MNI/ }));
  await next();
  await screen.findByLabelText('Lesion mask in MNI space (.nii.gz)');
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  window.localStorage.clear();
});

describe('example lesion in the wizard', () => {
  it('shows no button when the deployment offers no examples', async () => {
    mockApi({ routes: { 'GET /v1/example-inputs': () => jsonResponse([]) } });
    renderWizard();
    await toFilesStep();
    expect(screen.queryByRole('button', { name: EXAMPLE_BUTTON })).not.toBeInTheDocument();
  });

  it('shows no button when the example list cannot be read', async () => {
    mockApi();
    renderWizard();
    await toFilesStep();
    expect(screen.queryByRole('button', { name: EXAMPLE_BUTTON })).not.toBeInTheDocument();
  });

  it('attaches the example instead of uploading a file and starts the run', async () => {
    const { calls: all } = mockApi({
      run: makeRun({ input_set: 'mni-mask' }),
      routes: {
        'GET /v1/example-inputs': () => jsonResponse([EXAMPLE]),
        'POST /v1/cases/c1/inputs/lesion_mni/example': () =>
          jsonResponse(
            {
              id: 'i3',
              role: 'lesion_mni',
              artefact_type: 'lesion_mask_mni_any',
              created_at: '2026-11-14T10:00:00Z',
              origin: { kind: 'external-tool', tool: 'stroke-server-example', version: 'ex-left' },
            },
            201,
          ),
      },
    });
    renderWizard();
    await toFilesStep();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();

    await userEvent.click(await screen.findByRole('button', { name: EXAMPLE_BUTTON }));
    expect(screen.getByText('Synthetic example selected: Left hemisphere, medium')).toBeInTheDocument();
    // The declared-origin form is not offered: the server marks the example itself.
    expect(screen.queryByLabelText('Origin of the mask')).not.toBeInTheDocument();
    await next();
    await screen.findByRole('group', { name: 'Analysis' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled());
    await next();
    await screen.findByRole('combobox', { name: 'Functional disconnection' });
    const startButton = screen.getByRole('button', { name: 'Create case and start run' });
    await waitFor(() => expect(startButton).toBeEnabled());
    await userEvent.click(startButton);

    expect(await screen.findByRole('heading', { name: 'Run' })).toBeInTheDocument();
    expect(all.filter((c) => c.method === 'PUT')).toHaveLength(0);
    const attached = calls(all, 'POST', '/v1/cases/c1/inputs/lesion_mni/example');
    expect(attached).toHaveLength(1);
    expect(JSON.parse(attached[0].body as string)).toEqual({ example_id: 'ex-left' });
  });

  it('choosing a file replaces the example and the example can be removed', async () => {
    mockApi({ routes: { 'GET /v1/example-inputs': () => jsonResponse([EXAMPLE]) } });
    renderWizard();
    await toFilesStep();

    await userEvent.click(await screen.findByRole('button', { name: EXAMPLE_BUTTON }));
    await userEvent.click(screen.getByRole('button', { name: 'Remove' }));
    expect(screen.getByRole('button', { name: EXAMPLE_BUTTON })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: EXAMPLE_BUTTON }));
    const file = new File(['x'], 'mine.nii.gz', { type: 'application/gzip' });
    await userEvent.upload(screen.getByLabelText('Lesion mask in MNI space (.nii.gz)'), file);
    expect(screen.queryByText(/Synthetic example selected/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('Origin of the mask')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
  });
});
