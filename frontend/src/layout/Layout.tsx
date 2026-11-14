import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '../features/auth/AuthContext';
import { useMessage } from '../shared/messages';
import { LanguageSwitch } from './LanguageSwitch';

const LOGO_SRC = '/brand/biobizkaia-logo.png';

/** Institution logo; falls back to a text wordmark when the image is not deployed. */
function InstitutionLogo() {
  const { t } = useTranslation();
  const [failed, setFailed] = useState(false);
  if (failed) return <span className="wordmark">{t('brand.institution')}</span>;
  return <img className="institution-logo" src={LOGO_SRC} alt={t('brand.institution')} onError={() => setFailed(true)} />;
}

function Landing() {
  const { t } = useTranslation();
  const features = [
    { title: t('app.featureLesionTitle'), text: t('app.featureLesion') },
    { title: t('app.featureNetworkTitle'), text: t('app.featureNetwork') },
    { title: t('app.featureProvenanceTitle'), text: t('app.featureProvenance') },
  ];
  return (
    <section className="landing">
      <div className="landing-copy">
        <p className="eyebrow">{t('app.eyebrow')}</p>
        <h1>{t('app.title')}</h1>
        <p className="landing-lead">{t('app.subtitle')}</p>
        <ol className="pipeline-steps">
          {features.map((feature, index) => (
            <li key={feature.title}>
              <span className="step-index">{index + 1}</span>
              <div>
                <strong>{feature.title}</strong>
                <p>{feature.text}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>
      <div className="landing-form">
        <Outlet />
        <p className="landing-note">{t('app.loginPrompt')}</p>
      </div>
    </section>
  );
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
            <span className="stage-badge" title={t('brand.alphaHint')}>
              {t('brand.alpha')}
            </span>
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
        {user && (
          <div className="subnav">
            <nav className="main-nav" aria-label={t('nav.label')}>
              <NavLink to="/studies">{t('nav.studies')}</NavLink>
              <NavLink to="/cases">{t('nav.cases')}</NavLink>
              <NavLink to="/notifications">{t('nav.notifications')}</NavLink>
              {user.role === 'admin' && <NavLink to="/admin">{t('nav.admin')}</NavLink>}
            </nav>
            <span className="session-info">
              {t('app.sessionActive', { name: user.full_name, role: user.role === 'admin' ? 'admin' : 'researcher' })}
            </span>
          </div>
        )}
      </header>

      {user ? (
        <main className="page">
          {message && (
            <section className="card message-card">
              <p className="message">{message}</p>
            </section>
          )}
          <Outlet />
        </main>
      ) : (
        <main className="page page-landing">
          <Landing />
        </main>
      )}

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
