import { useTranslation } from 'react-i18next';
import type { AdminDashboard } from '../../api/types';
import { formatBytes, sumValues } from '../../shared/format';
import { Status } from '../../shared/Status';
import { useFormatters } from '../../shared/useFormatters';

function MetricCard({ label, value }: { label: string; value: string | number }) {
  return (
    <article className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function StatusBreakdown({ values }: { values: Record<string, number> }) {
  const { t } = useTranslation();
  const entries = Object.entries(values || {});
  if (entries.length === 0) return <p className="muted">{t('admin.noData')}</p>;
  return (
    <div className="breakdown-list">
      {entries.map(([status, count]) => (
        <div key={status}>
          <Status value={status} />
          <strong>{count}</strong>
        </div>
      ))}
    </div>
  );
}

export function DashboardCard({ dashboard, onRefresh }: { dashboard: AdminDashboard; onRefresh: () => void }) {
  const { t } = useTranslation();
  const { date } = useFormatters();
  return (
    <section className="card admin-dashboard">
      <div className="section-title">
        <div>
          <p className="eyebrow dark">{t('admin.eyebrow')}</p>
          <h2>{t('admin.dashboardTitle')}</h2>
        </div>
        <button className="secondary" onClick={onRefresh}>
          {t('admin.refreshDashboard')}
        </button>
      </div>

      <div className="metric-grid">
        <MetricCard
          label={t('admin.activeUsers')}
          value={`${dashboard.users.active}/${dashboard.users.total}`}
        />
        <MetricCard label={t('admin.studies')} value={sumValues(dashboard.studies_by_status)} />
        <MetricCard label={t('admin.uploads')} value={formatBytes(dashboard.storage.studies_bytes)} />
        <MetricCard label={t('admin.diskFree')} value={formatBytes(dashboard.storage.disk_free_bytes)} />
      </div>

      <h3>{t('admin.studiesByStatus')}</h3>
      <StatusBreakdown values={dashboard.studies_by_status} />

      <p className="hint">
        {t('admin.lastUpdate', {
          date: date(dashboard.generated_at),
          free: formatBytes(dashboard.storage.disk_free_bytes),
        })}
      </p>
    </section>
  );
}
