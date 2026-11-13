import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '../features/auth/AuthContext';
import { useMessage } from '../shared/messages';
import { LanguageSwitch } from './LanguageSwitch';

const LOGO_SRC = '/brand/institution-logo.svg';

/** Institution logo; falls back to a text wordmark when the image is not deployed. */
function InstitutionLogo() {
  const { t } = useTranslation();
  const [failed, setFailed] = useState(false);
  if (failed) return <span className="wordmark">{t('brand.institution')}</span>;
  return <img className="institution-logo" src={LOGO_SRC} alt={t('brand.institution')} onError={() => setFailed(true)} />;
}

export function Layout() {
  const { t } = useTranslation();
  const { user, logout } = useAuth();
  const { message } = useMessage();

  return (
    <div className="shell">
      <header className="site-header">
        <div className="site-header-inner">
          <div className="brand">
            <InstitutionLogo />
            <span className="brand-divider" aria-hidden="true" />
            <span className="product-name">{t('brand.product')}</span>
          </div>
          <div className="topbar-actions">
            <LanguageSwitch />
            {user && (
              <button className="secondary compact" onClick={() => void logout()}>
                {t('common.logout')}
              </button>
            )}
          </div>
        </div>
      </header>

      <main className="page">
        <section className={user ? 'hero hero-compact' : 'hero'}>
          <p className="eyebrow">{t('app.eyebrow')}</p>
          <h1>{t('app.title')}</h1>
          {!user && <p className="hero-lead">{t('app.subtitle')}</p>}
          {!user && (
            <ul className="feature-list">
              <li>{t('app.featureLesion')}</li>
              <li>{t('app.featureNetwork')}</li>
              <li>{t('app.featureProvenance')}</li>
            </ul>
          )}
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

      <footer className="site-footer">
        <p className="research-only">{t('app.researchOnly')}</p>
        <p>
          {t('brand.footer')} ·{' '}
          <a href="https://github.com/compneurobilbao/stroke-server-biobizkaia" target="_blank" rel="noreferrer">
            {t('brand.source')}
          </a>
        </p>
      </footer>
    </div>
  );
}
