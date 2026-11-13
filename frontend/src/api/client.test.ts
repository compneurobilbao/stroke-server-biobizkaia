import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, createApiClient, errorText, formatApiDetail } from './client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function stubFetch(response: Response | (() => Response)) {
  const fetchMock = vi.fn(async () => (typeof response === 'function' ? response() : response));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function lastCall(fetchMock: ReturnType<typeof stubFetch>): [string, RequestInit] {
  const call = fetchMock.mock.calls.at(-1) as unknown as [string, RequestInit];
  return call;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('createApiClient', () => {
  it('prefixes the base URL and sends the bearer token', async () => {
    const fetchMock = stubFetch(jsonResponse({ ok: true }));
    const api = createApiClient({ baseUrl: '/api', getToken: () => 'tok' });
    await api.json('/studies');
    const [url, init] = lastCall(fetchMock);
    expect(url).toBe('/api/studies');
    expect(init.headers).toMatchObject({ Authorization: 'Bearer tok' });
  });

  it('omits Authorization when there is no token or auth is disabled', async () => {
    const fetchMock = stubFetch(jsonResponse({}));
    const api = createApiClient({ baseUrl: '/api', getToken: () => '' });
    await api.json('/studies');
    expect(lastCall(fetchMock)[1].headers).not.toHaveProperty('Authorization');

    const authed = createApiClient({ baseUrl: '/api', getToken: () => 'tok' });
    await authed.json('/auth/login', { auth: false });
    expect(lastCall(fetchMock)[1].headers).not.toHaveProperty('Authorization');
  });

  it('serialises json bodies with a JSON content type', async () => {
    const fetchMock = stubFetch(jsonResponse({}));
    const api = createApiClient({ baseUrl: '/api', getToken: () => '' });
    await api.json('/users', { method: 'POST', json: { email: 'a@b.c' } });
    const [, init] = lastCall(fetchMock);
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ email: 'a@b.c' }));
    expect(init.headers).toMatchObject({ 'Content-Type': 'application/json' });
  });

  it('calls onUnauthorized on 401 for authenticated requests only', async () => {
    stubFetch(() => jsonResponse({ detail: 'expired' }, 401));
    const onUnauthorized = vi.fn();
    const api = createApiClient({ baseUrl: '/api', getToken: () => 'tok', onUnauthorized });
    await expect(api.json('/studies')).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    await expect(api.json('/auth/login', { auth: false })).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('surfaces detail and passes code through when present', async () => {
    stubFetch(jsonResponse({ detail: 'Invalid credentials', code: 'auth.invalid_credentials' }, 400));
    const api = createApiClient({ baseUrl: '/api', getToken: () => '' });
    const error = await api.json('/auth/login').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(400);
    expect(apiError.message).toBe('Invalid credentials');
    expect(apiError.detail).toBe('Invalid credentials');
    expect(apiError.code).toBe('auth.invalid_credentials');
  });

  it('works without a code field', async () => {
    stubFetch(jsonResponse({ detail: 'Nope' }, 403));
    const api = createApiClient({ baseUrl: '/api', getToken: () => '' });
    const error = (await api.json('/x').catch((e: unknown) => e)) as ApiError;
    expect(error.message).toBe('Nope');
    expect(error.code).toBeUndefined();
  });

  it('tolerates non-JSON error and success bodies', async () => {
    stubFetch(() => new Response('boom', { status: 500 }));
    const api = createApiClient({ baseUrl: '/api', getToken: () => '' });
    const error = (await api.json('/x').catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe('');
    expect(errorText(error, 'fallback')).toBe('fallback');

    stubFetch(() => new Response('', { status: 200 }));
    await expect(api.json('/x')).resolves.toEqual({});
  });

  it('returns blobs and raises ApiError for failed downloads', async () => {
    stubFetch(() => new Response('pdf-bytes', { status: 200 }));
    const api = createApiClient({ baseUrl: '/api', getToken: () => 'tok' });
    const blob = await api.blob('/studies/1/download/pdf');
    expect(await blob.text()).toBe('pdf-bytes');

    stubFetch(jsonResponse({ detail: 'Not found' }, 404));
    await expect(api.blob('/studies/1/download/pdf')).rejects.toMatchObject({ status: 404, message: 'Not found' });
  });
});

describe('formatApiDetail', () => {
  it('handles strings, validation arrays and objects', () => {
    expect(formatApiDetail('plain')).toBe('plain');
    expect(formatApiDetail([{ msg: 'first' }, { msg: 'second' }])).toBe('first second');
    expect(formatApiDetail({ msg: 'object' })).toBe('object');
    expect(formatApiDetail(undefined)).toBe('');
  });
});
