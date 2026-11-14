import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../../App';
import { jsonResponse, makeRun, mockApi } from '../../test/casesMock';
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

describe('run page', () => {
  it('shows the timeline and a cancel button for an active run', async () => {
    const { calls } = mockApi({
      routes: { 'POST /v1/runs/r1/cancel': () => jsonResponse({ run_id: 'r1', status: 'running', cancel_requested: true }, 202) },
    });
    renderRun();
    expect(await screen.findByRole('list', { name: 'Step timeline' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Retry|Resume/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel run' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/runs/r1/cancel')).toBe(true));
  });

  it('offers retry for a failed run and posts resume', async () => {
    const { calls } = mockApi({
      run: makeRun({ status: 'failed', failure_message: 'step failed', finished_at: '2026-11-14T10:10:00Z' }),
      routes: { 'POST /v1/runs/r1/resume': () => jsonResponse({ run_id: 'r1', status: 'queued' }, 202) },
    });
    renderRun();
    await userEvent.click(await screen.findByRole('button', { name: 'Retry from failed step' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/runs/r1/resume')).toBe(true));
    expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
  });

  it('offers resume for a canceled run', async () => {
    mockApi({ run: makeRun({ status: 'canceled' }) });
    renderRun();
    expect(await screen.findByRole('button', { name: 'Resume from canceled step' })).toBeInTheDocument();
  });

  it('lists the report and artefacts without showing stored file names', async () => {
    mockApi({
      run: makeRun({ status: 'succeeded' }),
      routes: {
        'GET /v1/runs/r1/reports': () =>
          jsonResponse([{ id: 'rep12345', template: 'research-report', language: 'en', pdf_artifact_id: 'p1', created_at: '2026-11-14T10:20:00Z' }]),
        'GET /v1/runs/r1/artifacts': () =>
          jsonResponse([
            { id: 'art12345', port: 'lesion_mask_mni', artefact_type: 'lesion_mask_mni', filename: 'secret-name.nii.gz', size_bytes: 2048, synthetic: false },
          ]),
      },
    });
    renderRun();
    expect(await screen.findByRole('button', { name: 'Download PDF report' })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Download lesion_mask_mni' })).toBeInTheDocument();
    expect(screen.queryByText(/secret-name/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Cancel|Retry|Resume/ })).not.toBeInTheDocument();
  });
});
