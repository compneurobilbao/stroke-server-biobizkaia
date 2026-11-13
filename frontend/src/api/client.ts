/**
 * Shared API client. Wraps `fetch`, attaches the bearer token, reports 401
 * responses and turns non-2xx JSON responses into `ApiError`.
 *
 * `detail` is surfaced as before. `code` (a machine-readable error code that
 * the backend may add next to `detail`) is passed through when present; nothing
 * in the UI depends on it.
 */

export function formatApiDetail(detail: unknown): string {
  if (!detail) return '';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const msg = (item as { msg?: unknown } | null)?.msg;
        return typeof msg === 'string' && msg ? msg : String(item);
      })
      .join(' ');
  }
  const msg = (detail as { msg?: unknown }).msg;
  return typeof msg === 'string' ? msg : '';
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly code: string | undefined;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    const record = payload !== null && typeof payload === 'object' ? (payload as Record<string, unknown>) : {};
    super(formatApiDetail(record.detail));
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
    this.detail = record.detail;
    this.code = typeof record.code === 'string' ? record.code : undefined;
  }
}

/** Text to show for a failed call: the server `detail` when there is one, else the fallback. */
export function errorText(error: unknown, fallback: string): string {
  return error instanceof ApiError && error.message ? error.message : fallback;
}

export interface RequestOptions {
  method?: string;
  /** JSON body; sets the Content-Type header. */
  json?: unknown;
  /** Raw body (for example FormData). */
  body?: BodyInit | null;
  headers?: Record<string, string>;
  /** Send the bearer token and react to 401. Defaults to true. */
  auth?: boolean;
}

export interface ApiClientOptions {
  baseUrl: string;
  getToken: () => string;
  onUnauthorized?: () => void;
}

export interface ApiClient {
  request(path: string, options?: RequestOptions): Promise<Response>;
  json<T>(path: string, options?: RequestOptions): Promise<T>;
  blob(path: string, options?: RequestOptions): Promise<Blob>;
}

export function createApiClient({ baseUrl, getToken, onUnauthorized }: ApiClientOptions): ApiClient {
  async function request(path: string, options: RequestOptions = {}): Promise<Response> {
    const { json, body, method, headers = {}, auth = true } = options;
    const finalHeaders: Record<string, string> = {};
    const token = auth ? getToken() : '';
    if (token) finalHeaders.Authorization = `Bearer ${token}`;
    if (json !== undefined) finalHeaders['Content-Type'] = 'application/json';
    Object.assign(finalHeaders, headers);
    const init: RequestInit = { headers: finalHeaders };
    if (method) init.method = method;
    if (json !== undefined) init.body = JSON.stringify(json);
    else if (body !== undefined) init.body = body;
    const response = await fetch(`${baseUrl}${path}`, init);
    if (auth && response.status === 401) onUnauthorized?.();
    return response;
  }

  async function json<T>(path: string, options?: RequestOptions): Promise<T> {
    const response = await request(path, options);
    const payload: unknown = await response.json().catch(() => undefined);
    if (!response.ok) throw new ApiError(response.status, payload);
    return (payload ?? {}) as T;
  }

  async function blob(path: string, options?: RequestOptions): Promise<Blob> {
    const response = await request(path, options);
    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => undefined);
      throw new ApiError(response.status, payload);
    }
    return response.blob();
  }

  return { request, json, blob };
}
