import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { jsonResponse, mockApi } from '../../test/casesMock';
import { TOKEN_KEY } from '../auth/AuthContext';
import type { EngineLayerInput, ViewerEngine } from './engine';

// jsdom has no WebGL: the rendering engine is replaced; everything else is the real page.
const factory = vi.hoisted(() => ({ create: vi.fn() }));
vi.mock('./engine', () => ({ createNiivueEngine: factory.create }));

const GZIP = new Uint8Array([0x1f, 0x8b, 8, 0, 1, 2, 3]);

function fakeEngine() {
  const engine = {
    load: vi.fn(async (layers: EngineLayerInput[]) =>
      layers.map((layer) => ({
        kind: layer.kind,
        min: 0,
        max: layer.kind === 'disconnection' ? 8 : 1,
        threshold: layer.kind === 'disconnection' ? 2 : 0,
        opacity: 0.7,
      })),
    ),
    setOpacity: vi.fn(),
    setThreshold: vi.fn(),
    destroy: vi.fn(),
  } satisfies ViewerEngine;
  factory.create.mockResolvedValue(engine);
  return engine;
}

const art = (id: string, type: string, synthetic = false) => ({
  id,
  port: type,
  artefact_type: type,
  filename: 'secret-name.nii.gz',
  size_bytes: 7,
  synthetic,
});

function routes(artifacts: unknown[]) {
  return {
    'GET /v1/runs/r1/artifacts': () => jsonResponse(artifacts),
    'GET /v1/runs/r1/artifacts/c1/download': () => new Response(GZIP.slice(), { status: 200 }),
    'GET /v1/runs/r1/artifacts/l1/download': () => new Response(GZIP.slice(), { status: 200 }),
    'GET /v1/runs/r1/artifacts/d1/download': () => new Response(GZIP.slice(), { status: 200 }),
  };
}

function renderViewer() {
  window.localStorage.setItem(TOKEN_KEY, 'jwt-123');
  return render(
    <MemoryRouter initialEntries={['/runs/r1/view']}>
      <App />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  factory.create.mockReset();
  window.localStorage.clear();
});

describe('viewer page', () => {
  it('downloads the artefacts with the bearer token header and passes the bytes to the engine', async () => {
    const engine = fakeEngine();
    const { fetchMock } = mockApi({
      routes: routes([art('c1', 'ct_mni'), art('l1', 'lesion_mask_mni'), art('d1', 'fdisconnection_map', true)]),
    });
    renderViewer();

    await waitFor(() => expect(engine.load).toHaveBeenCalledTimes(1));
    const layers = engine.load.mock.calls[0][0];
    expect(layers.map((layer) => [layer.kind, layer.name, layer.data.byteLength])).toEqual([
      ['template', 'template.nii.gz', GZIP.length],
      ['lesion', 'lesion.nii.gz', GZIP.length],
      ['disconnection', 'disconnection.nii.gz', GZIP.length],
    ]);

    const downloads = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/download'));
    expect(downloads).toHaveLength(3);
    for (const [url, init] of downloads) {
      expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer jwt-123');
      expect(String(url)).not.toContain('jwt-123');
      expect(String(url)).not.toMatch(/token|access/i);
    }

    expect(await screen.findByRole('group', { name: /Background: CT in MNI space/ })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: /Disconnection map/ })).toHaveTextContent('synthetic');
    expect(screen.queryByTestId('no-template')).not.toBeInTheDocument();
    expect(screen.queryByText(/secret-name/)).not.toBeInTheDocument();
  });

  it('says so when there is no background template', async () => {
    fakeEngine();
    mockApi({ routes: routes([art('l1', 'lesion_mask_mni'), art('d1', 'fdisconnection_map')]) });
    renderViewer();
    expect(await screen.findByTestId('no-template')).toHaveTextContent(/No anatomical background/);
    expect(screen.queryByRole('group', { name: /Background/ })).not.toBeInTheDocument();
  });

  it('drives opacity, visibility and threshold of a layer through the engine', async () => {
    const engine = fakeEngine();
    mockApi({ routes: routes([art('l1', 'lesion_mask_mni'), art('d1', 'fdisconnection_map')]) });
    renderViewer();

    const map = await screen.findByRole('group', { name: /Disconnection map/ });
    const opacity = screen.getByRole('slider', { name: 'Opacity of Disconnection map' });
    const threshold = screen.getByRole('slider', { name: 'Threshold of Disconnection map' });
    expect((threshold as HTMLInputElement).value).toBe('2');
    expect(threshold).toHaveAttribute('max', '8');

    fireEvent.change(opacity, { target: { value: '0.3' } });
    expect(engine.setOpacity).toHaveBeenLastCalledWith('disconnection', 0.3);
    fireEvent.change(threshold, { target: { value: '4' } });
    expect(engine.setThreshold).toHaveBeenLastCalledWith('disconnection', 4);

    const show = map.querySelector('input[type="checkbox"]') as HTMLInputElement;
    fireEvent.click(show);
    expect(engine.setOpacity).toHaveBeenLastCalledWith('disconnection', 0);
    fireEvent.click(show);
    expect(engine.setOpacity).toHaveBeenLastCalledWith('disconnection', 0.3);

    // The background has no threshold control.
    expect(screen.queryByRole('slider', { name: /Threshold of Background/ })).not.toBeInTheDocument();
  });

  it('shows a message when the run has nothing to view and loads no engine', async () => {
    mockApi({ routes: routes([art('c1', 'ct_mni'), art('r1', 'report_pdf')]) });
    renderViewer();
    expect(await screen.findByText(/no lesion mask or disconnection map/)).toBeInTheDocument();
    expect(factory.create).not.toHaveBeenCalled();
  });

  it('reports a failed download instead of an empty canvas', async () => {
    fakeEngine();
    mockApi({
      routes: {
        ...routes([art('l1', 'lesion_mask_mni')]),
        'GET /v1/runs/r1/artifacts/l1/download': () =>
          jsonResponse({ detail: 'File is gone.', code: 'file_missing_in_storage' }, 404),
      },
    });
    renderViewer();
    expect(await screen.findByRole('alert')).toHaveTextContent('File is gone.');
    expect(factory.create).not.toHaveBeenCalled();
  });

  it('reports an engine failure (for example no WebGL2)', async () => {
    factory.create.mockRejectedValue(new Error('WebGL2 is not available'));
    mockApi({ routes: routes([art('l1', 'lesion_mask_mni')]) });
    renderViewer();
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the images.');
  });

  it('releases the engine when the page is left', async () => {
    const engine = fakeEngine();
    mockApi({ routes: routes([art('l1', 'lesion_mask_mni')]) });
    const { unmount } = renderViewer();
    await screen.findByRole('group', { name: /Lesion mask/ });
    unmount();
    expect(engine.destroy).toHaveBeenCalledTimes(1);
  });
});
