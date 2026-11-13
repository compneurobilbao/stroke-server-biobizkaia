import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createApiClient, type ApiClient } from '../../api/client';
import type { LoginResponse, User } from '../../api/types';
import i18n from '../../i18n';
import { useMessage } from '../../shared/messages';

export const TOKEN_KEY = 'neuroimagen_access_token';
export const API_BASE: string = import.meta.env.VITE_API_BASE || '/api';

interface AuthState {
  token: string;
  user: User | null;
  /** True while a stored token is being exchanged for the current user. */
  bootstrapping: boolean;
  api: ApiClient;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

function readStoredToken(): string {
  try {
    return window.localStorage.getItem(TOKEN_KEY) || '';
  } catch {
    return '';
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const { setMessage } = useMessage();
  const [token, setToken] = useState<string>(readStoredToken);
  const [user, setUser] = useState<User | null>(null);
  const [bootstrapping, setBootstrapping] = useState<boolean>(() => readStoredToken() !== '');
  const tokenRef = useRef(token);
  tokenRef.current = token;
  const userRef = useRef(user);
  userRef.current = user;

  const clearSession = useCallback(() => {
    window.localStorage.removeItem(TOKEN_KEY);
    tokenRef.current = '';
    setToken('');
    setUser(null);
    setMessage('');
  }, [setMessage]);

  const api = useMemo(
    () =>
      createApiClient({
        baseUrl: API_BASE,
        getToken: () => tokenRef.current,
        onUnauthorized: () => {
          clearSession();
          setMessage(i18n.t('auth.sessionExpired'));
        },
      }),
    [clearSession, setMessage],
  );

  const refreshUser = useCallback(async () => {
    if (!tokenRef.current) return;
    try {
      setUser(await api.json<User>('/auth/me'));
    } catch {
      // Keep the current state, as before: a failed lookup does not log the user out.
    }
  }, [api]);

  useEffect(() => {
    if (!token || userRef.current) {
      setBootstrapping(false);
      return;
    }
    let cancelled = false;
    setBootstrapping(true);
    void refreshUser().finally(() => {
      if (!cancelled) setBootstrapping(false);
    });
    return () => {
      cancelled = true;
    };
  }, [token, refreshUser]);

  const login = useCallback(
    async (email: string, password: string) => {
      const payload = await api.json<LoginResponse>('/auth/login', {
        method: 'POST',
        json: { email, password },
        auth: false,
      });
      window.localStorage.setItem(TOKEN_KEY, payload.access_token);
      tokenRef.current = payload.access_token;
      userRef.current = payload.user;
      setUser(payload.user);
      setToken(payload.access_token);
    },
    [api],
  );

  const logout = useCallback(async () => {
    if (tokenRef.current) {
      await api.request('/auth/logout', { method: 'POST' }).catch(() => null);
    }
    clearSession();
  }, [api, clearSession]);

  const value = useMemo(
    () => ({ token, user, bootstrapping, api, login, logout, refreshUser }),
    [token, user, bootstrapping, api, login, logout, refreshUser],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}
