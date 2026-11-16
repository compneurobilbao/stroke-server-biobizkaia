import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router-dom';
import { errorText } from '../../api/client';
import {
  cancelRun,
  downloadArtifact,
  downloadReportPdf,
  getRun,
  getStepLog,
  isActive,
  listArtifacts,
  listReports,
  resumeRun,
  type Artifact,
  type Report,
  type RunDetail,
} from '../../api/v1';
import { formatBytes } from '../../shared/format';
import { useMessage } from '../../shared/messages';
import { useFormatters } from '../../shared/useFormatters';
import { useAuth } from '../auth/AuthContext';
import { RunStatusBadge } from './RunStatusBadge';
import { RunTimeline } from './RunTimeline';
import { selectLayers } from '../viewer/layers';

const POLL_INTERVAL_MS = 3000;

/** Extension of a platform-generated artefact name, used only to name the saved file. */
function extensionOf(filename: string): string {
  const match = /(\.nii\.gz|\.[A-Za-z0-9]{1,5})$/.exec(filename);
  return match ? match[1] : '';
}

function saveBlob(blob: Blob, name: string) {
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  window.URL.revokeObjectURL(url);
}

export function RunPage() {
  const { runId = '' } = useParams();
  const { t } = useTranslation();
  const { api } = useAuth();
  const { setMessage } = useMessage();
  const { date } = useFormatters();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [loadError, setLoadError] = useState('');
  const [acting, setActing] = useState(false);

  const load = useCallback(async () => {
    try {
      const detail = await getRun(api, runId);
      setRun(detail);
      setLoadError('');
      const [artifactList, reportList] = await Promise.all([
        listArtifacts(api, runId).catch(() => [] as Artifact[]),
        listReports(api, runId).catch(() => [] as Report[]),
      ]);
      setArtifacts(artifactList);
      setReports(reportList);
    } catch (error) {
      setLoadError(errorText(error, t('cases.run.loadFailed')));
    }
  }, [api, runId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const active = run ? isActive(run.status) : false;
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => void load(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [active, load]);

  async function act(kind: 'cancel' | 'resume') {
    setActing(true);
    try {
      await (kind === 'cancel' ? cancelRun(api, runId) : resumeRun(api, runId));
      setMessage(kind === 'cancel' ? t('cases.run.cancelRequested') : t('cases.run.resumeQueued'));
      await load();
    } catch (error) {
      setMessage(errorText(error, kind === 'cancel' ? t('cases.run.cancelFailed') : t('cases.run.resumeFailed')));
    } finally {
      setActing(false);
    }
  }

  async function downloadOne(artifact: Artifact) {
    try {
      const blob = await downloadArtifact(api, runId, artifact.id);
      saveBlob(blob, `${artifact.artefact_type}-${artifact.id.slice(0, 8)}${extensionOf(artifact.filename)}`);
    } catch (error) {
      setMessage(errorText(error, t('cases.run.downloadFailed')));
    }
  }

  async function downloadReport(report: Report) {
    try {
      saveBlob(await downloadReportPdf(api, runId, report.id), `report-${report.id.slice(0, 8)}.pdf`);
    } catch (error) {
      setMessage(errorText(error, t('cases.run.downloadFailed')));
    }
  }

  if (!run) {
    return (
      <section className="card">
        <p className={loadError ? 'message' : 'hint'} role={loadError ? 'alert' : undefined}>
          {loadError || t('cases.run.loading')}
        </p>
        <Link to="/cases">{t('cases.backToCases')}</Link>
      </section>
    );
  }

  const viewable = selectLayers(artifacts).some((layer) => layer.kind !== 'template');
  const canResume = run.status === 'failed' || run.status === 'canceled';
  const slots = run.slots ?? [];
  const steps = run.steps ?? [];

  return (
    <>
      <section className="card" aria-labelledby="run-title">
        <div className="section-title">
          <h2 id="run-title">{t('cases.run.title')}</h2>
          <RunStatusBadge state={run.status} />
        </div>
        <dl className="detail-grid">
          <dt>{t('cases.run.pipeline')}</dt>
          <dd>
            {run.pipeline_id} <code>{run.pipeline_version}</code>
          </dd>
          <dt>{t('cases.run.inputSet')}</dt>
          <dd>{run.input_set}</dd>
          <dt>{t('cases.run.created')}</dt>
          <dd>{date(run.created_at)}</dd>
          <dt>{t('cases.run.finished')}</dt>
          <dd>{date(run.finished_at)}</dd>
          {run.failure_message && (
            <>
              <dt>{t('cases.run.failure')}</dt>
              <dd>{run.failure_message}</dd>
            </>
          )}
        </dl>
        {(run.cancel_requested || run.cancel_requested_at) && active && (
          <p className="hint">{t('cases.run.cancelPending')}</p>
        )}
        <div className="wizard-actions">
          {active && (
            <button type="button" className="danger" disabled={acting} onClick={() => void act('cancel')}>
              {t('cases.run.cancel')}
            </button>
          )}
          {canResume && (
            <button type="button" disabled={acting} onClick={() => void act('resume')}>
              {run.status === 'failed' ? t('cases.run.retry') : t('cases.run.resume')}
            </button>
          )}
          {viewable && (
            <Link className="download" to={`/runs/${run.id}/view`}>
              {t('cases.run.view')}
            </Link>
          )}
          <Link className="download secondary" to={`/cases/${run.case_id}`}>
            {t('cases.run.openCase')}
          </Link>
        </div>
        <p className="hint">{t('cases.run.researchOnly')}</p>
      </section>

      <section className="card">
        <h2>{t('cases.run.timeline')}</h2>
        <RunTimeline slots={slots} steps={steps} loadLog={(stepId) => getStepLog(api, runId, stepId)} />
      </section>

      <section className="card">
        <h2>{t('cases.run.reports')}</h2>
        {reports.length === 0 ? (
          <p className="hint">{t('cases.run.noReports')}</p>
        ) : (
          <ul className="plain-list">
            {reports.map((report) => (
              <li key={report.id}>
                <span>
                  {report.template} ({report.language}) - {date(report.created_at)}
                </span>{' '}
                <button type="button" onClick={() => void downloadReport(report)}>
                  {t('cases.run.downloadReport')}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="card">
        <h2>{t('cases.run.artifacts')}</h2>
        {artifacts.length === 0 ? (
          <p className="hint">{t('cases.run.noArtifacts')}</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t('cases.run.artifactPort')}</th>
                  <th>{t('cases.run.artifactType')}</th>
                  <th>{t('cases.run.artifactSize')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {artifacts.map((artifact) => (
                  <tr key={artifact.id}>
                    <td>{artifact.port}</td>
                    <td>
                      {artifact.artefact_type}
                      {artifact.synthetic && <span className="tag tag-warn">{t('cases.run.synthetic')}</span>}
                    </td>
                    <td>{artifact.size_bytes == null ? '' : formatBytes(artifact.size_bytes)}</td>
                    <td>
                      <button
                        type="button"
                        className="secondary compact"
                        aria-label={t('cases.run.downloadArtifact', { port: artifact.port })}
                        onClick={() => void downloadOne(artifact)}
                      >
                        {t('cases.run.download')}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
