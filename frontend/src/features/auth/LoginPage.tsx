import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Navigate } from 'react-router-dom';
import { errorText } from '../../api/client';
import { useMessage } from '../../shared/messages';
import { useAuth } from './AuthContext';

export function LoginPage() {
  const { t } = useTranslation();
  const { user, login } = useAuth();
  const { message, setMessage } = useMessage();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);

  if (user) return <Navigate to="/studies" replace />;

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setMessage(t('auth.loggingIn'));
    try {
      await login(email, password);
      setPassword('');
      setMessage(t('auth.loggedIn'));
    } catch (error) {
      setMessage(errorText(error, t('auth.failed')));
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="card auth-card">
      <h2>{t('auth.title')}</h2>
      <form className="auth-form" onSubmit={handleLogin}>
        <label className="field">
          <span>{t('auth.emailLabel')}</span>
          <input
            type="email"
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder={t('auth.emailPlaceholder')}
            required
          />
        </label>
        <label className="field">
          <span>{t('auth.passwordLabel')}</span>
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder={t('auth.passwordPlaceholder')}
            required
          />
        </label>
        <button disabled={loading}>{loading ? t('auth.submitting') : t('auth.submit')}</button>
      </form>
      <p className="hint">{t('auth.hint')}</p>
      {message && <p className="message">{message}</p>}
    </section>
  );
}
