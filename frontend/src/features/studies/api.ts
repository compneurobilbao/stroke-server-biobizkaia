import type { ApiClient } from '../../api/client';
import type { MessageResponse, ShareLink, Study, StudyDetail, StudyLogs } from '../../api/types';

export type StudyAction = 'cancel' | 'retry' | 'delete';
export type ArtifactType = 'pdf' | 'zip';

export const listStudies = (api: ApiClient) => api.json<Study[]>('/studies');

export const uploadStudy = (api: ApiClient, file: File, bidsSubjectId: string) => {
  const formData = new FormData();
  formData.append('file', file);
  if (bidsSubjectId) formData.append('bids_subject_id', bidsSubjectId);
  return api.json<MessageResponse>('/studies/upload', { method: 'POST', body: formData });
};

export const downloadArtifact = (api: ApiClient, studyId: string, type: ArtifactType) =>
  api.blob(`/studies/${studyId}/download/${type}`);

export const getStudyDetail = (api: ApiClient, studyId: string) => api.json<StudyDetail>(`/studies/${studyId}/detail`);

export const getStudyLogs = (api: ApiClient, studyId: string) => api.json<StudyLogs>(`/studies/${studyId}/logs?lines=200`);

export const runStudyAction = (api: ApiClient, studyId: string, action: StudyAction) =>
  api.json<MessageResponse>(`/studies/${studyId}${action === 'delete' ? '' : `/${action}`}`, {
    method: action === 'delete' ? 'DELETE' : 'POST',
  });

export const updateClinicalReview = (api: ApiClient, studyId: string, clinicalReviewStatus: string) =>
  api.json<Study>(`/studies/${studyId}/clinical-review`, {
    method: 'PATCH',
    json: { clinical_review_status: clinicalReviewStatus },
  });

export const listShareLinks = (api: ApiClient, studyId: string) => api.json<ShareLink[]>(`/studies/${studyId}/share-links`);

export const createShareLink = (api: ApiClient, studyId: string) =>
  api.json<ShareLink>(`/studies/${studyId}/share-links`, { method: 'POST' });

export const revokeShareLink = (api: ApiClient, studyId: string, linkId: string) =>
  api.json<ShareLink>(`/studies/${studyId}/share-links/${linkId}/revoke`, { method: 'POST' });
