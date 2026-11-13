import { useTranslation } from 'react-i18next';
import type { Role, StudyDetail } from '../../api/types';
import { Status } from '../../shared/Status';
import { useFormatters } from '../../shared/useFormatters';

export function StudyDetailCard({ detail, role, onClose }: { detail: StudyDetail; role: Role; onClose: () => void }) {
  const { t } = useTranslation();
  const { date } = useFormatters();
  const isAdmin = role === 'admin';
  const review = detail.clinical_review_status || 'technical_only';

  return (
    <section className="card detail-card">
      <div className="section-title">
        <h2>{t('studies.detail.title')}</h2>
        <button className="secondary" onClick={onClose}>
          {t('common.close')}
        </button>
      </div>
      <dl className="detail-grid">
        <dt>{t('common.id')}</dt>
        <dd>{detail.id}</dd>
        <dt>{t('studies.detail.file')}</dt>
        <dd>{detail.original_filename}</dd>
        <dt>{t('studies.detail.status')}</dt>
        <dd>
          <Status value={detail.status} error={detail.error_message} warnings={detail.processing_warnings} />
        </dd>
        <dt>{t('studies.detail.subject')}</dt>
        <dd>{detail.bids_subject_id || t('common.notApplicable')}</dd>
        <dt>{t('studies.detail.review')}</dt>
        <dd>{t(`clinicalReview.${review}`, { defaultValue: t('clinicalReview.technical_only') })}</dd>
        {!isAdmin && (
          <>
            <dt>{t('studies.detail.pipeline')}</dt>
            <dd>{detail.processor_backend || t('common.notInformed')}</dd>
          </>
        )}
        <dt>{t('studies.detail.checksum')}</dt>
        <dd>{detail.checksum || t('common.notInformed')}</dd>
      </dl>
      {!isAdmin && (
        <>
          <h3>{t('studies.detail.jobs')}</h3>
          <div className="table-wrap small-table">
            <table>
              <thead>
                <tr>
                  <th>{t('common.id')}</th>
                  <th>{t('studies.detail.status')}</th>
                  <th>{t('studies.detail.attempt')}</th>
                  <th>{t('studies.detail.worker')}</th>
                  <th>{t('studies.detail.start')}</th>
                  <th>{t('studies.detail.end')}</th>
                </tr>
              </thead>
              <tbody>
                {detail.jobs.map((job) => (
                  <tr key={job.id}>
                    <td>
                      <code>{job.id.slice(0, 8)}</code>
                    </td>
                    <td>{job.status}</td>
                    <td>{job.retry_count}</td>
                    <td>{job.worker_name || <span className="muted">{t('common.notAssigned')}</span>}</td>
                    <td>
                      {job.started_at ? date(job.started_at) : <span className="muted">{t('common.pending')}</span>}
                    </td>
                    <td>
                      {job.finished_at ? date(job.finished_at) : <span className="muted">{t('common.pending')}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
