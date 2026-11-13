import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { errorText } from '../../api/client';
import type { Study, StudyDetail, StudyLogs } from '../../api/types';
import { useMessage } from '../../shared/messages';
import { useAuth } from '../auth/AuthContext';
import {
  downloadArtifact,
  getStudyDetail,
  getStudyLogs,
  listStudies,
  runStudyAction,
  updateClinicalReview,
  type ArtifactType,
  type StudyAction,
} from './api';
import { LogsCard } from './LogsCard';
import { ShareCard } from './ShareCard';
import { StudiesTable } from './StudiesTable';
import { StudyDetailCard } from './StudyDetailCard';
import { UploadCard } from './UploadCard';

const POLL_INTERVAL_MS = 5000;

export function StudiesPage() {
  const { t } = useTranslation();
  const { api, user, refreshUser } = useAuth();
  const { setMessage } = useMessage();
  const [studies, setStudies] = useState<Study[]>([]);
  const [detail, setDetail] = useState<StudyDetail | null>(null);
  const [logs, setLogs] = useState<StudyLogs | null>(null);
  const [shareStudy, setShareStudy] = useState<Study | null>(null);

  const loadStudies = useCallback(async () => {
    try {
      setStudies(await listStudies(api));
    } catch {
      // Polling failures are silent, as before.
    }
  }, [api]);

  useEffect(() => {
    void loadStudies();
    const timer = window.setInterval(() => void loadStudies(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [loadStudies]);

  if (!user) return null;

  async function handleDownload(study: Study, type: ArtifactType) {
    try {
      const blob = await downloadArtifact(api, study.id, type);
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download =
        type === 'pdf'
          ? t('studies.pdfFilename', { id: study.id })
          : t('studies.zipFilename', { id: study.id });
      link.click();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      setMessage(errorText(error, t('studies.downloadFailed')));
    }
  }

  async function handleDetail(study: Study) {
    try {
      setDetail(await getStudyDetail(api, study.id));
      setLogs(null);
    } catch (error) {
      setMessage(errorText(error, t('studies.detail.failed')));
    }
  }

  async function handleLogs(study: Study) {
    try {
      setLogs(await getStudyLogs(api, study.id));
      setDetail(null);
    } catch (error) {
      setMessage(errorText(error, t('studies.logs.failed')));
    }
  }

  async function handleAction(study: Study, action: StudyAction) {
    const confirmations: Record<StudyAction, string> = {
      cancel:
        study.status === 'processing'
          ? t('studies.confirm.cancelProcessing')
          : t('studies.confirm.cancelQueued'),
      retry: t('studies.confirm.retry'),
      delete: t('studies.confirm.delete'),
    };
    if (!window.confirm(confirmations[action])) return;
    try {
      const payload = await runStudyAction(api, study.id, action);
      setMessage(payload.message || t('studies.actionDone'));
      setDetail(null);
      setLogs(null);
      setShareStudy(null);
      await loadStudies();
      await refreshUser();
    } catch (error) {
      setMessage(errorText(error, t('studies.actionFailed')));
    }
  }

  async function handleReviewChange(study: Study, value: string) {
    try {
      const payload = await updateClinicalReview(api, study.id, value);
      setStudies((current) => current.map((item) => (item.id === payload.id ? payload : item)));
      setDetail((current) =>
        current && current.id === payload.id
          ? { ...current, clinical_review_status: payload.clinical_review_status }
          : current,
      );
      setMessage(t('studies.reviewUpdated'));
    } catch (error) {
      setMessage(errorText(error, t('studies.reviewFailed')));
    }
  }

  return (
    <>
      {user.role !== 'admin' && <UploadCard user={user} onUploaded={loadStudies} />}
      <StudiesTable
        studies={studies}
        role={user.role}
        onRefresh={() => void loadStudies()}
        onReviewChange={(study, value) => void handleReviewChange(study, value)}
        onDownload={(study, type) => void handleDownload(study, type)}
        onDetail={(study) => void handleDetail(study)}
        onLogs={(study) => void handleLogs(study)}
        onShare={setShareStudy}
        onAction={(study, action) => void handleAction(study, action)}
      />
      {detail && <StudyDetailCard detail={detail} role={user.role} onClose={() => setDetail(null)} />}
      {shareStudy && <ShareCard study={shareStudy} onClose={() => setShareStudy(null)} />}
      {logs && <LogsCard logs={logs} onClose={() => setLogs(null)} />}
    </>
  );
}
