import { useTranslation } from 'react-i18next';
import { NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '../features/auth/AuthContext';
import { useMessage } from '../shared/messages';
import { LanguageSwitch } from './LanguageSwitch';

export function Layout() {
  const { t } = useTranslation();
  const { user, logout } = useAuth();
  const { message } = useMessage();

  return (
    <main className="page">
      <section className="hero">
        <div className="topbar">
          <p className="eyebrow">{t('app.eyebrow')}</p>
          <div className="topbar-actions">
            <LanguageSwitch />
            {user && (
              <button className="secondary light" onClick={() => void logout()}>
                {t('common.logout')}
              </button>
            )}
          </div>
        </div>
        <h1>{t('app.title')}</h1>
        <p>{t('app.subtitle')}</p>
        {user ? (
          <div className="notice">{t('app.sessionActive', { name: user.full_name, role: user.role === 'admin' ? 'admin' : 'researcher' })}</div>
        ) : (
          <div className="notice">{t('app.loginPrompt')}</div>
        )}
        {user && (
          <nav className="main-nav" aria-label={t('nav.label')}>
            <NavLink to="/studies">{t('nav.studies')}</NavLink>
            <NavLink to="/notifications">{t('nav.notifications')}</NavLink>
            {user.role === 'admin' && <NavLink to="/admin">{t('nav.admin')}</NavLink>}
          </nav>
        )}
      </section>

      {user && message && (
        <section className="card message-card">
          <p className="message">{message}</p>
        </section>
      )}

      <Outlet />
    </main>
  );
}
