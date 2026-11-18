import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, createApiClient } from './client';
import {
  AGE_BANDS,
  ageToBand,
  cancelRun,
  createCase,
  createRun,
  getConfig,
  getStepLog,
  listPresets,
  listSlots,
  previewPlan,
  putInput,
  resumeRun,
} from './v1';

function stubFetch(response: Response) {
  const fetchMock = vi.fn(async () => response.clone());
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

const api = createApiClient({ baseUrl: '/api', getToken: () => 'tok' });

function lastCall(fetchMock: ReturnType<typeof stubFetch>): [string, RequestInit] {
  return fetchMock.mock.calls.at(-1) as unknown as [string, RequestInit];
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('age bands', () => {
  it('lists the 19 valid bands, the last one collapsed', () => {
    expect(AGE_BANDS).toHaveLength(19);
    expect(AGE_BANDS[0]).toBe('0-4');
    expect(AGE_BANDS[17]).toBe('85-89');
    expect(AGE_BANDS[18]).toBe('90-104');
  });

  it('converts an exact age to its band in the browser', () => {
    expect(ageToBand(0)).toBe('0-4');
    expect(ageToBand(4)).toBe('0-4');
    expect(ageToBand(5)).toBe('5-9');
    expect(ageToBand(67)).toBe('65-69');
    expect(ageToBand(89)).toBe('85-89');
    expect(ageToBand(90)).toBe('90-104');
    expect(ageToBand(104)).toBe('90-104');
  });

  it('refuses ages that are not whole numbers in range', () => {
    expect(ageToBand(-1)).toBeNull();
    expect(ageToBand(105)).toBeNull();
    expect(ageToBand(67.5)).toBeNull();
    expect(ageToBand(Number.NaN)).toBeNull();
  });
});

describe('v1 client', () => {
  it('creates a case with sex and age band only', async () => {
    const fetchMock = stubFetch(new Response(JSON.stringify({ id: 'c1' }), { status: 201 }));
    await createCase(api, { sex: 'female', age_band: '60-64' });
    const [url, init] = lastCall(fetchMock);
    expect(url).toBe('/api/v1/cases');
    expect(init.method).toBe('POST');
    expect(init.headers).toMatchObject({ Authorization: 'Bearer tok', 'Content-Type': 'application/json' });
    expect(JSON.parse(init.body as string)).toEqual({ sex: 'female', age_band: '60-64' });
  });

  it('uploads an input as multipart field "file" and nothing else', async () => {
    const fetchMock = stubFetch(new Response(JSON.stringify({ id: 'i1' }), { status: 201 }));
    await putInput(api, 'c1', 'lesion_mni', new File(['x'], 'patient-name.nii.gz'));
    const [url, init] = lastCall(fetchMock);
    expect(url).toBe('/api/v1/cases/c1/inputs/lesion_mni');
    expect(init.method).toBe('PUT');
    const form = init.body as FormData;
    expect([...form.keys()]).toEqual(['file']);
  });

  it('starts a run with the chosen implementations and input roles', async () => {
    const fetchMock = stubFetch(new Response(JSON.stringify({ id: 'r1' }), { status: 201 }));
    await createRun(api, 'c1', {
      pipeline_id: 'stroke-lnm',
      input_roles: ['lesion_mni'],
      implementations: { 'functional-disconnection': 'fdc-alt' },
    });
    const [url, init] = lastCall(fetchMock);
    expect(url).toBe('/api/v1/cases/c1/runs');
    expect(JSON.parse(init.body as string)).toEqual({
      pipeline_id: 'stroke-lnm',
      input_roles: ['lesion_mni'],
      implementations: { 'functional-disconnection': 'fdc-alt' },
    });
  });

  it('encodes path segments', async () => {
    const fetchMock = stubFetch(new Response('[]'));
    await listSlots(api, 'a/b', '1.0.0');
    expect(lastCall(fetchMock)[0]).toBe('/api/v1/pipelines/a%2Fb/versions/1.0.0/slots');
  });

  it('posts cancel and resume', async () => {
    const fetchMock = stubFetch(new Response(JSON.stringify({ run_id: 'r1', status: 'canceled' })));
    await cancelRun(api, 'r1');
    expect(lastCall(fetchMock)[0]).toBe('/api/v1/runs/r1/cancel');
    expect(lastCall(fetchMock)[1].method).toBe('POST');
    await resumeRun(api, 'r1');
    expect(lastCall(fetchMock)[0]).toBe('/api/v1/runs/r1/resume');
    expect(lastCall(fetchMock)[1].method).toBe('POST');
  });

  it('reads a step log with its truncation flag', async () => {
    stubFetch(new Response('line 1\n', { headers: { 'X-Log-Truncated': 'true' } }));
    await expect(getStepLog(api, 'r1', 'a1')).resolves.toEqual({ text: 'line 1\n', truncated: true });
    stubFetch(new Response('line 1\n'));
    await expect(getStepLog(api, 'r1', 'a1')).resolves.toEqual({ text: 'line 1\n', truncated: false });
  });

  it('surfaces the error code of a refused request', async () => {
    stubFetch(
      new Response(JSON.stringify({ detail: 'No implementation', code: 'no_implementation', slot: 'normalisation' }), {
        status: 409,
      }),
    );
    const error = await createRun(api, 'c1', { pipeline_id: 'stroke-lnm' }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe('no_implementation');
    expect((error as ApiError).status).toBe(409);

    stubFetch(new Response(JSON.stringify({ detail: 'Log not available', code: 'log_not_available' }), { status: 404 }));
    await expect(getStepLog(api, 'r1', 'a1')).rejects.toMatchObject({ code: 'log_not_available', status: 404 });
  });
});

describe('v1 client: run plans', () => {
  it('reads the configuration of the caller', async () => {
    const fetchMock = stubFetch(
      new Response(JSON.stringify({ deployment_profile: 'on-premise', run_plan_policy: 'free_for_researchers', free_mode_allowed: true })),
    );
    await expect(getConfig(api)).resolves.toMatchObject({ free_mode_allowed: true });
    expect(lastCall(fetchMock)[0]).toBe('/api/v1/config');
  });

  it('lists the presets of a pipeline version', async () => {
    const fetchMock = stubFetch(new Response('[]'));
    await listPresets(api, 'stroke-lnm', '0.3.0');
    expect(lastCall(fetchMock)[0]).toBe('/api/v1/pipelines/stroke-lnm/versions/0.3.0/presets');
  });

  it('posts a plan preview with the request as body and returns a refused plan like any other answer', async () => {
    const answer = { feasible: false, mode: 'free', input_set: 'mni-mask', slots: [], reasons: [{ code: 'empty_request', message: 'm' }] };
    const fetchMock = stubFetch(new Response(JSON.stringify(answer)));
    const request = { input_roles: ['lesion_mni'], targets: { required: ['report'], if_available: [] }, implementations: {} };
    await expect(previewPlan(api, 'stroke-lnm', '0.3.0', request)).resolves.toEqual(answer);
    const [url, init] = lastCall(fetchMock);
    expect(url).toBe('/api/v1/pipelines/stroke-lnm/versions/0.3.0/plan');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual(request);
  });

  it('sends a preset or targets with the run request', async () => {
    const fetchMock = stubFetch(new Response(JSON.stringify({ id: 'r1' }), { status: 201 }));
    await createRun(api, 'c1', { pipeline_id: 'stroke-lnm', preset: { id: 'full', version: '1.0.0' } });
    expect(JSON.parse(lastCall(fetchMock)[1].body as string)).toEqual({
      pipeline_id: 'stroke-lnm',
      preset: { id: 'full', version: '1.0.0' },
    });
    await createRun(api, 'c1', { pipeline_id: 'stroke-lnm', targets: { required: ['report'], if_available: [] } });
    expect(JSON.parse(lastCall(fetchMock)[1].body as string)).toEqual({
      pipeline_id: 'stroke-lnm',
      targets: { required: ['report'], if_available: [] },
    });
  });

  it('keeps the reasons of a refused plan on the error', async () => {
    const reasons = [{ code: 'target_unreachable', message: 'm', target: 'report' }];
    stubFetch(new Response(JSON.stringify({ detail: 'd', code: 'target_unreachable', reasons }), { status: 422 }));
    const error = await createRun(api, 'c1', { pipeline_id: 'stroke-lnm' }).catch((e: unknown) => e);
    expect((error as ApiError).payload).toMatchObject({ reasons });
  });
});

