import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { errorText } from '../../api/client';
import { getCase, listCases, type CaseRead, type InputFile } from '../../api/v1';
import { useFormatters } from '../../shared/useFormatters';
import { useAuth } from '../auth/AuthContext';
import { originText } from './RunPlanSummary';

export function CasesPage() {
  const { t } = useTranslation();
  const { api } = useAuth();
  const { date } = useFormatters();
  const [cases, setCases] = useState<CaseRead[] | null>(null);
  // Inputs of each case (the list endpoint has none): loaded after the list, one request per case; a case
  // whose detail cannot be loaded keeps its row and shows no origin.
  const [inputs, setInputs] = useState<Record<string, InputFile[] | null>>({});
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    listCases(api)
      .then((list) => {
        if (cancelled) return;
        setCases(list);
        for (const item of list) {
          getCase(api, item.id)
            .then((detail) => {
              if (!cancelled) setInputs((previous) => ({ ...previous, [item.id]: detail.inputs ?? [] }));
            })
            .catch(() => {
              if (!cancelled) setInputs((previous) => ({ ...previous, [item.id]: null }));
            });
        }
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
                <th>{t('cases.list.origin')}</th>
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
                  <td data-testid={`origin-${item.id}`}>
                    <CaseOrigins inputs={inputs[item.id]} />
                  </td>
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

/** Declared origins of a case's inputs, as the user said them (not verified); undefined while loading, null when unknown. */
function CaseOrigins({ inputs }: { inputs: InputFile[] | null | undefined }) {
  const { t } = useTranslation();
  if (inputs === undefined) return <span className="hint">...</span>;
  if (inputs === null) return <span className="hint">{t('common.notInformed')}</span>;
  const declared = inputs.filter((input) => input.origin);
  if (declared.length === 0) return <span className="hint">{t('cases.list.noOrigin')}</span>;
  return (
    <ul className="plain-list">
      {declared.map((input) => (
        <li key={input.role}>
          {t(`cases.roles.${input.role}`, { defaultValue: input.role })}: {input.origin ? originText(t, input.origin) : ''}
        </li>
      ))}
    </ul>
  );
}
