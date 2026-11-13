import { useState, type FormEvent } from 'react';
import { Trans, useTranslation } from 'react-i18next';
import { errorText } from '../../api/client';
import type { User } from '../../api/types';
import { formatBytes } from '../../shared/format';
import { useMessage } from '../../shared/messages';
import { useFormatters } from '../../shared/useFormatters';
import { useAuth } from '../auth/AuthContext';
import { uploadStudy } from './api';

export function UploadCard({ user, onUploaded }: { user: User; onUploaded: () => Promise<void> }) {
  const { t } = useTranslation();
  const { api, refreshUser } = useAuth();
  const { setMessage } = useMessage();
  const { quota } = useFormatters();
  const [file, setFile] = useState<File | null>(null);
  const [subjectId, setSubjectId] = useState('');
  const [loading, setLoading] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file) {
      setMessage(t('studies.upload.selectFile'));
      return;
    }
    const normalizedSubject = subjectId.trim();
    if (normalizedSubject && !/^sub-[A-Za-z0-9]+$/.test(normalizedSubject)) {
      setMessage(t('studies.upload.invalidSubject'));
      return;
    }
    setLoading(true);
    setMessage(t('studies.upload.uploading'));
    try {
      const payload = await uploadStudy(api, file, normalizedSubject);
      setFile(null);
      setSubjectId('');
      setMessage(payload.message || t('studies.upload.queued'));
      await onUploaded();
      await refreshUser();
    } catch (error) {
      setMessage(errorText(error, t('studies.upload.failed')));
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="card">
      <h2>{t('studies.upload.title')}</h2>
      <p className="hint">
        <Trans i18nKey="studies.upload.hintFormat" components={{ strong: <strong />, code: <code /> }} />
      </p>
      <p className="hint">
        <Trans
          i18nKey="studies.upload.hintStorage"
          values={{ used: formatBytes(user.storage_used_bytes), quota: quota(user.storage_quota_bytes) }}
          components={{ strong: <strong /> }}
        />
      </p>
      <form onSubmit={handleSubmit}>
        <input type="file" accept=".nii.gz" onChange={(event) => setFile(event.target.files?.[0] || null)} />
        <input
          type="text"
          value={subjectId}
          onChange={(event) => setSubjectId(event.target.value)}
          placeholder={t('studies.upload.subjectPlaceholder')}
          aria-label={t('studies.upload.subjectLabel')}
        />
        <button disabled={loading}>{loading ? t('studies.upload.working') : t('studies.upload.submit')}</button>
      </form>
      <p className="hint">{t('studies.upload.hintDuration')}</p>
    </section>
  );
}
