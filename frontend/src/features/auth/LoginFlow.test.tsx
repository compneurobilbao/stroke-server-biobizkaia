import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { TOKEN_KEY } from './AuthContext';

const USER = {
  id: 'u1',
  email: 'ana@example.org',
  full_name: 'Ana Test',
  role: 'researcher',
  is_active: true,
  storage_used_bytes: 0,
  storage_quota_bytes: null,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

type Handler = (init: RequestInit | undefined) => Response;

function mockApi(routes: Record<string, Handler>) {
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    const path = url.replace(/^\/api/, '').split('?')[0];
    const key = `${init?.method ?? 'GET'} ${path}`;
    const handler = routes[key];
    return handler ? handler(init) : jsonResponse([]);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderApp(initialPath = '/') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <App />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('login flow', () => {
  it('redirects anonymous visitors to the login form', async () => {
    mockApi({});
    renderApp('/studies');
    expect(await screen.findByRole('heading', { name: 'Login' })).toBeInTheDocument();
  });

  it('signs in, stores the token and lands on the studies page', async () => {
    const fetchMock = mockApi({
      'POST /auth/login': () => jsonResponse({ access_token: 'jwt-123', user: USER }),
      'GET /studies': () => jsonResponse([]),
    });
    const user = userEvent.setup();
    renderApp('/');

    await user.type(await screen.findByPlaceholderText('email@institution.org'), 'ana@example.org');
    await user.type(screen.getByPlaceholderText('Password'), 'secret-pass');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByText('Active session: Ana Test (researcher).')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'My studies' })).toBeInTheDocument();
    expect(window.localStorage.getItem(TOKEN_KEY)).toBe('jwt-123');

    const loginCall = fetchMock.mock.calls.find(([url]) => String(url) === '/api/auth/login');
    expect(loginCall).toBeDefined();
    const init = loginCall?.[1];
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({ email: 'ana@example.org', password: 'secret-pass' });

    const studiesCall = fetchMock.mock.calls.find(([url]) => String(url) === '/api/studies');
    expect(studiesCall?.[1]?.headers).toMatchObject({ Authorization: 'Bearer jwt-123' });
  });

  it('shows the server detail when the login fails', async () => {
    mockApi({
      'POST /auth/login': () => jsonResponse({ detail: 'Incorrect email or password', code: 'auth.bad_credentials' }, 401),
    });
    const user = userEvent.setup();
    renderApp('/login');

    await user.type(await screen.findByPlaceholderText('email@institution.org'), 'ana@example.org');
    await user.type(screen.getByPlaceholderText('Password'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByText('Incorrect email or password')).toBeInTheDocument();
    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it('falls back to a generic message when the error has no detail', async () => {
    mockApi({ 'POST /auth/login': () => new Response('', { status: 500 }) });
    const user = userEvent.setup();
    renderApp('/login');

    await user.type(await screen.findByPlaceholderText('email@institution.org'), 'ana@example.org');
    await user.type(screen.getByPlaceholderText('Password'), 'x');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByText('Could not sign in.')).toBeInTheDocument();
  });

  it('restores the session from a stored token and signs out', async () => {
    window.localStorage.setItem(TOKEN_KEY, 'stored');
    mockApi({
      'GET /auth/me': () => jsonResponse(USER),
      'POST /auth/logout': () => jsonResponse({}),
    });
    const user = userEvent.setup();
    renderApp('/studies');

    expect(await screen.findByText('Active session: Ana Test (researcher).')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull());
    expect(await screen.findByRole('heading', { name: 'Login' })).toBeInTheDocument();
  });

  it('keeps non-admin users out of the admin route', async () => {
    window.localStorage.setItem(TOKEN_KEY, 'stored');
    mockApi({ 'GET /auth/me': () => jsonResponse(USER) });
    renderApp('/admin');
    expect(await screen.findByRole('heading', { name: 'My studies' })).toBeInTheDocument();
  });
});

describe('language switch', () => {
  it('switches the UI to Spanish and persists the choice', async () => {
    mockApi({});
    const user = userEvent.setup();
    renderApp('/login');

    await user.selectOptions(await screen.findByLabelText('Language'), 'es');

    expect(await screen.findByRole('button', { name: 'Entrar' })).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Contraseña')).toBeInTheDocument();
    expect(window.localStorage.getItem('neuroimagen_language')).toBe('es');
    expect(document.documentElement.lang).toBe('es');
  });
});
