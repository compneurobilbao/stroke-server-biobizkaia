import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../../App';
import { jsonResponse, mockApi, type Call } from '../../test/casesMock';
import { TOKEN_KEY } from '../auth/AuthContext';

const NOW = '2026-11-14T10:00:00Z';
const caseRead = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  owner_user_id: 'u1',
  subject_label: `sub-000${id.slice(1)}`,
  session_label: 'ses-01',
  created_at: NOW,
  updated_at: NOW,
  ...extra,
});
const input = (role: string, origin: unknown = null) => ({
  id: `i-${role}`,
  role,
  artefact_type: role === 'ct' ? 'ct_native' : 'lesion_mask_mni_any',
  created_at: NOW,
  origin,
});
const body = (call: Call) => JSON.parse(call.body as string) as Record<string, unknown>;

function renderAt(path: string) {
  window.localStorage.setItem(TOKEN_KEY, 'jwt-123');
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
});

describe('case list: declared origin', () => {
  it('shows what was declared for each case, none declared, and unknown when the detail cannot be loaded', async () => {
    mockApi({
      routes: {
        'GET /v1/cases': () => jsonResponse([caseRead('c1'), caseRead('c2'), caseRead('c3')]),
        'GET /v1/cases/c1': () =>
          jsonResponse({
            ...caseRead('c1'),
            inputs: [input('lesion_mni', { kind: 'external-tool', tool: 'example-tool', version: '2.1' })],
          }),
        'GET /v1/cases/c2': () => jsonResponse({ ...caseRead('c2'), inputs: [input('ct'), input('lesion_native')] }),
        'GET /v1/cases/c3': () => jsonResponse({ detail: 'boom', code: 'internal' }, 500),
      },
    });
    renderAt('/cases');
    expect(await screen.findByRole('columnheader', { name: 'Declared origin of the masks' })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId('origin-c1')).toHaveTextContent(
        'Lesion mask in MNI space (.nii.gz): external tool example-tool 2.1',
      ),
    );
    await waitFor(() => expect(screen.getByTestId('origin-c2')).toHaveTextContent('Not declared'));
    await waitFor(() => expect(screen.getByTestId('origin-c3')).toHaveTextContent('Not reported'));
    // the case rows are still there
    expect(screen.getByRole('link', { name: 'sub-0001 / ses-01' })).toBeInTheDocument();
  });

  it('shows a declared manual origin', async () => {
    mockApi({
      routes: {
        'GET /v1/cases': () => jsonResponse([caseRead('c1')]),
        'GET /v1/cases/c1': () => jsonResponse({ ...caseRead('c1'), inputs: [input('lesion_native', { kind: 'manual' })] }),
      },
    });
    renderAt('/cases');
    await waitFor(() => expect(screen.getByTestId('origin-c1')).toHaveTextContent(': drawn by hand'));
  });
});

describe('case page: edit the declared origin before the first run', () => {
  function caseRoutes(options: { owner?: string; runs?: unknown[]; origin?: unknown; patch?: () => Response } = {}) {
    return {
      'GET /v1/cases/c1': () =>
        jsonResponse({
          ...caseRead('c1', { owner_user_id: options.owner ?? 'u1' }),
          inputs: [input('ct'), input('lesion_native', options.origin ?? null)],
        }),
      'GET /v1/cases/c1/runs': () => jsonResponse(options.runs ?? []),
      'PATCH /v1/cases/c1/inputs/lesion_native':
        options.patch ?? (() => jsonResponse(input('lesion_native', { kind: 'manual' }))),
    };
  }
  const lesionRow = () => screen.getByTestId('input-lesion_native');

  it('lets the owner declare an external tool, sending only the origin', async () => {
    const { calls } = mockApi({
      routes: caseRoutes({
        patch: () => jsonResponse(input('lesion_native', { kind: 'external-tool', tool: 'example-tool', version: '1.0' })),
      }),
    });
    renderAt('/cases/c1');
    await screen.findByTestId('input-lesion_native');
    // only the mask can carry an origin, not the CT
    expect(screen.getAllByRole('button', { name: 'Edit origin' })).toHaveLength(1);
    expect(within(screen.getByTestId('input-ct')).queryByRole('button', { name: 'Edit origin' })).not.toBeInTheDocument();

    await userEvent.click(within(lesionRow()).getByRole('button', { name: 'Edit origin' }));
    await userEvent.selectOptions(screen.getByLabelText('Origin of the mask'), 'external-tool');
    expect(screen.getByRole('button', { name: 'Save origin' })).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Tool name'), 'example-tool');
    await userEvent.type(screen.getByLabelText('Tool version'), '1.0');
    await userEvent.click(screen.getByRole('button', { name: 'Save origin' }));

    await waitFor(() =>
      expect(screen.getByTestId('origin-lesion_native')).toHaveTextContent(
        'Declared by the user (not verified): external tool example-tool 1.0',
      ),
    );
    const patch = calls.filter((c) => c.method === 'PATCH');
    expect(patch).toHaveLength(1);
    expect(patch[0].path).toBe('/v1/cases/c1/inputs/lesion_native');
    expect(body(patch[0])).toEqual({ origin: { kind: 'external-tool', tool: 'example-tool', version: '1.0' } });
    // the form is closed again
    expect(screen.queryByRole('button', { name: 'Save origin' })).not.toBeInTheDocument();
  });

  it('starts from the stored origin and clears it with "Not declared"', async () => {
    const { calls } = mockApi({
      routes: caseRoutes({ origin: { kind: 'manual' }, patch: () => jsonResponse(input('lesion_native', null)) }),
    });
    renderAt('/cases/c1');
    await screen.findByTestId('origin-lesion_native');
    await userEvent.click(within(lesionRow()).getByRole('button', { name: 'Edit origin' }));
    expect(screen.getByLabelText('Origin of the mask')).toHaveValue('manual');
    await userEvent.selectOptions(screen.getByLabelText('Origin of the mask'), '');
    await userEvent.click(screen.getByRole('button', { name: 'Save origin' }));
    await waitFor(() => expect(screen.queryByTestId('origin-lesion_native')).not.toBeInTheDocument());
    expect(body(calls.filter((c) => c.method === 'PATCH')[0])).toEqual({ origin: null });
  });

  it('cancelling sends nothing', async () => {
    const { calls } = mockApi({ routes: caseRoutes() });
    renderAt('/cases/c1');
    await screen.findByTestId('input-lesion_native');
    await userEvent.click(within(lesionRow()).getByRole('button', { name: 'Edit origin' }));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('button', { name: 'Save origin' })).not.toBeInTheDocument();
    expect(calls.filter((c) => c.method === 'PATCH')).toHaveLength(0);
  });

  it('offers no editing once the case has a run, and says why', async () => {
    mockApi({
      routes: caseRoutes({
        origin: { kind: 'manual' },
        runs: [
          {
            id: 'r1',
            case_id: 'c1',
            pipeline_id: 'stroke-lnm',
            pipeline_version: '0.1.0',
            input_set: 'mni-mask',
            status: 'succeeded',
            created_at: NOW,
          },
        ],
      }),
    });
    renderAt('/cases/c1');
    await screen.findByTestId('origin-lesion_native');
    expect(screen.queryByRole('button', { name: 'Edit origin' })).not.toBeInTheDocument();
    expect(screen.getByTestId('origin-locked')).toHaveTextContent('cannot be changed after the first run');
  });

  it('offers no editing to a user who is not the owner', async () => {
    mockApi({ routes: caseRoutes({ owner: 'someone-else', origin: { kind: 'manual' } }) });
    renderAt('/cases/c1');
    await screen.findByTestId('origin-lesion_native');
    expect(screen.queryByRole('button', { name: 'Edit origin' })).not.toBeInTheDocument();
  });

  it('shows the refusal when the server says the case already has a run', async () => {
    mockApi({
      routes: caseRoutes({
        patch: () => jsonResponse({ detail: 'Inputs cannot change after the first run', code: 'case_has_runs' }, 409),
      }),
    });
    renderAt('/cases/c1');
    await screen.findByTestId('input-lesion_native');
    await userEvent.click(within(lesionRow()).getByRole('button', { name: 'Edit origin' }));
    await userEvent.selectOptions(screen.getByLabelText('Origin of the mask'), 'manual');
    await userEvent.click(screen.getByRole('button', { name: 'Save origin' }));
    expect(await within(lesionRow()).findByRole('alert')).toHaveTextContent('Inputs cannot change after the first run');
    // the form stays open so that nothing typed is lost
    expect(screen.getByRole('button', { name: 'Save origin' })).toBeEnabled();
  });
});
