import { useTranslation } from 'react-i18next';
import type { StudyLogs } from '../../api/types';

export function LogsCard({ logs, onClose }: { logs: StudyLogs; onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <section className="card detail-card">
      <div className="section-title">
        <h2>{t('studies.logs.title')}</h2>
        <button className="secondary" onClick={onClose}>
          {t('common.close')}
        </button>
      </div>
      {logs.logs.length === 0 && <p className="muted">{t('studies.logs.empty')}</p>}
      {logs.logs.map((log) => (
        <article key={log.name} className="log-block">
          <h3>
            {log.name}
            {log.truncated ? ` ${t('studies.logs.truncated')}` : ''}
          </h3>
          <pre>{log.content || t('studies.logs.emptyLog')}</pre>
        </article>
      ))}
    </section>
  );
}
