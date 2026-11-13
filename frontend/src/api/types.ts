export type Role = 'admin' | 'researcher';

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  storage_used_bytes: number;
  storage_quota_bytes: number | null;
}

export interface LoginResponse {
  access_token: string;
  user: User;
}

export interface MessageResponse {
  message?: string;
}

export interface Job {
  id: string;
  status: string;
  retry_count: number;
  worker_name: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface Study {
  id: string;
  original_filename: string;
  bids_subject_id: string | null;
  status: string;
  error_message?: string | null;
  processing_warnings?: string | null;
  created_at: string;
  clinical_review_status?: string | null;
  has_pdf: boolean;
  has_output_zip: boolean;
}

export interface StudyDetail extends Study {
  processor_backend?: string | null;
  checksum?: string | null;
  jobs: Job[];
}

export interface StudyLog {
  name: string;
  truncated?: boolean;
  content?: string | null;
}

export interface StudyLogs {
  logs: StudyLog[];
}

export interface ShareLink {
  id: string;
  url?: string;
  created_at: string;
  expires_at: string;
  access_count: number;
  is_revoked: boolean;
  is_expired: boolean;
}

export interface NotificationItem {
  id: string;
  title: string;
  message: string;
  created_at: string;
  read_at: string | null;
  email_status: string;
}

export interface NotificationPrefs {
  notify_on_processing_completed: boolean;
  notify_on_processing_failed: boolean;
}

export interface AdminDashboard {
  users: { active: number; total: number };
  studies_by_status: Record<string, number>;
  storage: { studies_bytes: number; disk_free_bytes: number };
  generated_at: string;
}

export interface NewUserPayload {
  email: string;
  full_name: string;
  password: string;
  role: Role;
  storage_quota_bytes: number | null;
}
