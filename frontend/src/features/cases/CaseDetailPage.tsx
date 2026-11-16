import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router-dom';
import { errorText } from '../../api/client';
import { getCase, listCaseRuns, type CaseDetail, type RunRead } from '../../api/v1';
import { useFormatters } from '../../shared/useFormatters';
import { useAuth } from '../auth/AuthContext';
import { RunStatusBadge } from './RunStatusBadge';

export function CaseDetailPage() {
  const { caseId = '' } = useParams();
  const { t } = useTranslation();
  const { api } = useAuth();
  const { date } = useFormatters();
  const [item, setItem] = useState<CaseDetail | null>(null);
  const [runs, setRuns] = useState<RunRead[]>([]);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    Promise.all([getCase(api, caseId), listCaseRuns(api, caseId)])
      .then(([detail, runList]) => {
        if (cancelled) return;
        setItem(detail);
        setRuns(runList);
      })
      .catch((e) => {
        if (!cancelled) setError(errorText(e, t('cases.detail.failed')));
      });
    return () => {
      cancelled = true;
    };
  }, [api, caseId, t]);

  if (!item) {
    return (
      <section className="card">
        <p className={error ? 'message' : 'hint'} role={error ? 'alert' : undefined}>
          {error || t('cases.detail.loading')}
        </p>
        <Link to="/cases">{t('cases.backToCases')}</Link>
      </section>
    );
  }

  return (
    <>
      <section className="card">
        <div className="section-title">
          <h2>
            {item.subject_label} / {item.session_label}
          </h2>
          <Link className="download secondary" to="/cases">
            {t('cases.backToCases')}
          </Link>
        </div>
        <dl className="detail-grid">
          <dt>{t('cases.sex.label')}</dt>
          <dd>{item.sex ? t(`cases.sex.${item.sex}`) : t('common.notInformed')}</dd>
          <dt>{t('cases.ageBand')}</dt>
          <dd>{item.age_band ?? t('common.notInformed')}</dd>
          <dt>{t('cases.list.created')}</dt>
          <dd>{date(item.created_at)}</dd>
          <dt>{t('cases.detail.inputs')}</dt>
          <dd>
            {(item.inputs ?? []).length === 0
              ? t('common.notInformed')
              : (item.inputs ?? []).map((input) => t(`cases.roles.${input.role}`, { defaultValue: input.role })).join(', ')}
          </dd>
        </dl>
      </section>
      <section className="card">
        <h2>{t('cases.detail.runs')}</h2>
        {runs.length === 0 ? (
          <p className="hint">{t('cases.detail.noRuns')}</p>
        ) : (
          <ul className="plain-list">
            {runs.map((run) => (
              <li key={run.id}>
                <Link to={`/runs/${run.id}`}>
                  {run.pipeline_id} {run.pipeline_version} - {date(run.created_at)}
                </Link>{' '}
                <RunStatusBadge state={run.status} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
