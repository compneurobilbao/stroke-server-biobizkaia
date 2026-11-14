import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { errorText } from '../../api/client';
import { listCases, type CaseRead } from '../../api/v1';
import { useFormatters } from '../../shared/useFormatters';
import { useAuth } from '../auth/AuthContext';

export function CasesPage() {
  const { t } = useTranslation();
  const { api } = useAuth();
  const { date } = useFormatters();
  const [cases, setCases] = useState<CaseRead[] | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    listCases(api)
      .then((list) => {
        if (!cancelled) setCases(list);
      })
      .catch((e) => {
        if (!cancelled) setError(errorText(e, t('cases.list.failed')));
      });
    return () => {
      cancelled = true;
    };
  }, [api, t]);

  return (
    <section className="card">
      <div className="section-title">
        <h2>{t('cases.list.title')}</h2>
        <Link className="download" to="/cases/new">
          {t('cases.list.new')}
        </Link>
      </div>
      <p className="hint">{t('cases.list.hint')}</p>
      {error && <p className="message" role="alert">{error}</p>}
      {cases && cases.length === 0 && <p className="hint">{t('cases.list.empty')}</p>}
      {cases && cases.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{t('cases.list.case')}</th>
                <th>{t('cases.sex.label')}</th>
                <th>{t('cases.ageBand')}</th>
                <th>{t('cases.list.created')}</th>
              </tr>
            </thead>
            <tbody>
              {cases.map((item) => (
                <tr key={item.id}>
                  <td>
                    <Link to={`/cases/${item.id}`}>
                      {item.subject_label} / {item.session_label}
                    </Link>
                  </td>
                  <td>{item.sex ? t(`cases.sex.${item.sex}`) : t('common.notInformed')}</td>
                  <td>{item.age_band ?? t('common.notInformed')}</td>
                  <td>{date(item.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
