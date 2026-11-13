import { useTranslation } from 'react-i18next';
import type { Role, Study } from '../../api/types';
import { Status } from '../../shared/Status';
import { useFormatters } from '../../shared/useFormatters';
import type { ArtifactType, StudyAction } from './api';

interface StudiesTableProps {
  studies: Study[];
  role: Role;
  onRefresh: () => void;
  onReviewChange: (study: Study, value: string) => void;
  onDownload: (study: Study, type: ArtifactType) => void;
  onDetail: (study: Study) => void;
  onLogs: (study: Study) => void;
  onShare: (study: Study) => void;
  onAction: (study: Study, action: StudyAction) => void;
}

export function StudiesTable({
  studies,
  role,
  onRefresh,
  onReviewChange,
  onDownload,
  onDetail,
  onLogs,
  onShare,
  onAction,
}: StudiesTableProps) {
  const { t } = useTranslation();
  const { date } = useFormatters();
  const isAdmin = role === 'admin';

  return (
    <section className="card">
      <div className="section-title">
        <h2>{isAdmin ? t('studies.titleAdmin') : t('studies.titleResearcher')}</h2>
        <button className="secondary" onClick={onRefresh}>
          {t('common.refresh')}
        </button>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t('studies.columns.file')}</th>
              <th>{t('studies.columns.subject')}</th>
              <th>{t('studies.columns.status')}</th>
              <th>{t('studies.columns.date')}</th>
              <th>{t('studies.columns.review')}</th>
              <th>{t('studies.columns.results')}</th>
              <th>{t('studies.columns.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {studies.map((study) => (
              <tr key={study.id}>
                <td>{study.original_filename}</td>
                <td>{study.bids_subject_id || <span className="muted">{t('common.notApplicable')}</span>}</td>
                <td>
                  <Status value={study.status} error={study.error_message} warnings={study.processing_warnings} />
                </td>
                <td>{date(study.created_at)}</td>
                <td>
                  <select
                    value={study.clinical_review_status || 'technical_only'}
                    onChange={(event) => onReviewChange(study, event.target.value)}
                  >
                    <option value="technical_only">{t('clinicalReview.technical_only')}</option>
                    <option value="reviewed" disabled={study.status !== 'completed'}>
                      {t('clinicalReview.reviewed')}
                    </option>
                    <option value="validated" disabled={study.status !== 'completed'}>
                      {t('clinicalReview.validated')}
                    </option>
                  </select>
                </td>
                <td>
                  {study.has_pdf ? (
                    <button className="download" onClick={() => onDownload(study, 'pdf')}>
                      {t('studies.pdf')}
                    </button>
                  ) : (
                    <span className="muted">{t('studies.pdfUnavailable')}</span>
                  )}
                  {study.has_output_zip && (
                    <button className="download secondary-download" onClick={() => onDownload(study, 'zip')}>
                      {t('studies.zip')}
                    </button>
                  )}
                </td>
                <td className="actions-cell">
                  <button className="secondary compact" onClick={() => onDetail(study)}>
                    {t('studies.detailButton')}
                  </button>
                  <button className="secondary compact" onClick={() => onLogs(study)}>
                    {t('studies.logsButton')}
                  </button>
                  {study.has_pdf && study.status === 'completed' && (
                    <button className="secondary compact" onClick={() => onShare(study)}>
                      {t('studies.shareButton')}
                    </button>
                  )}
                  {!isAdmin && ['queued', 'processing'].includes(study.status) && (
                    <button className="secondary compact" onClick={() => onAction(study, 'cancel')}>
                      {t('studies.cancel')}
                    </button>
                  )}
                  {!isAdmin && study.status === 'failed' && (
                    <button className="secondary compact" onClick={() => onAction(study, 'retry')}>
                      {t('studies.retry')}
                    </button>
                  )}
                  {!['queued', 'processing'].includes(study.status) && (
                    <button className="danger compact" onClick={() => onAction(study, 'delete')}>
                      {t('studies.delete')}
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {studies.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  {t('studies.empty')}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
