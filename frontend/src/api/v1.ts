/**
 * Client for the versioned case and run API (`/api/v1`). Shapes follow the committed
 * OpenAPI snapshot `docs/api/openapi-v1.json`; only the fields the UI uses are typed.
 *
 * Privacy: a case carries a platform-generated label, an optional sex and an age band.
 * The exact age is never sent; `ageToBand` converts it in the browser.
 */
import { ApiError, type ApiClient } from './client';

export type RunStatus = 'queued' | 'running' | 'awaiting_review' | 'succeeded' | 'failed' | 'canceled';
export type Sex = 'female' | 'male';

export interface PipelineSummary {
  id: string;
  version: string;
  title: string;
  description?: string | null;
  inputs: { name: string; type: string; description?: string | null }[];
  input_sets: { name: string; description?: string | null; inputs: string[] }[];
}

export interface SlotImplementation {
  id: string;
  step_name: string;
  step_version: string;
  title?: string | null;
  available: boolean;
  enabled: boolean;
  is_default: boolean;
  is_pipeline_default: boolean;
  stub?: boolean | null;
}

export interface SlotPort {
  port: string;
  type: string;
  optional?: boolean;
}

export interface Slot {
  name: string;
  title?: string | null;
  description?: string | null;
  position: number;
  optional: boolean;
  produces?: SlotPort[];
  when?: { inputs_present?: string[] } | Record<string, unknown> | null;
  default_implementation?: string | null;
  implementations: SlotImplementation[];
}

export interface InputFile {
  id: string;
  role: string;
  artefact_type: string;
  size_bytes?: number | null;
  created_at: string;
  /** Declared by the uploader; not verified by the platform. */
  origin?: InputOrigin | null;
}

export interface CaseRead {
  id: string;
  subject_label: string;
  session_label: string;
  sex?: Sex | null;
  age_band?: string | null;
  created_at: string;
  updated_at: string;
  keep_forever?: boolean;
}

export interface CaseDetail extends CaseRead {
  inputs?: InputFile[];
}

export interface CaseCreate {
  sex?: Sex | null;
  age_band?: string | null;
}

export type PlanMode = 'full' | 'preset' | 'free';
export type PlanRole = 'target' | 'upstream' | 'if_available';
export type SlotStatus = 'selected' | 'skipped' | 'unavailable';
export type SlotReason = 'condition_false' | 'upstream_absent' | 'no_implementation' | 'not_requested';
export type ChosenBy = 'user' | 'deployment_default' | 'pipeline_default' | 'preset';

/** The external tool an implementation wraps (step contract 1.2 `upstream` block), copied into the run. */
export interface UpstreamInfo {
  name?: string;
  version?: string;
  licence?: string;
  homepage?: string;
  [key: string]: unknown;
}

export interface RunSlot {
  id: string;
  slot: string;
  position: number;
  optional: boolean;
  status: SlotStatus;
  implementation_id?: string | null;
  step_name?: string | null;
  step_version?: string | null;
  chosen_by?: ChosenBy | null;
  stub?: boolean | null;
  reason?: SlotReason | null;
  plan_role?: PlanRole | null;
  upstream?: UpstreamInfo | null;
}

export interface PlanPresetRef {
  id: string;
  version: string;
  sha256?: string;
}

/** Targets as `slot.port` lists (resolved) or as slot names (request). */
export interface PlanTargets {
  required: string[];
  if_available: string[];
}

export interface RunPlan {
  mode: PlanMode;
  preset?: PlanPresetRef | null;
  targets?: PlanTargets;
}

/** Origin of a supplied input as declared by the user (contract 1.2); not verified by the platform. */
export interface InputOrigin {
  kind: string;
  tool?: string;
  version?: string;
}

export interface RunInput {
  role: string;
  input_file_id?: string;
  origin?: InputOrigin | null;
}

export interface PlanReason {
  code: string;
  message: string;
  target?: string | null;
  slot?: string | null;
  port?: string | null;
  required_by?: string | null;
}

export interface PlanSlot {
  name: string;
  status: SlotStatus;
  reason?: SlotReason | null;
  plan_role?: PlanRole | null;
  required_by?: string | null;
  implementation?: string | null;
  chosen_by?: ChosenBy | null;
}

export interface PlanPreview {
  feasible: boolean;
  mode: PlanMode;
  input_set: string;
  preset?: PlanPresetRef | null;
  targets?: PlanTargets;
  slots?: PlanSlot[];
  reasons?: PlanReason[];
}

export interface PresetPlan {
  input_set: string;
  available: boolean;
  slots: PlanSlot[];
  reasons?: PlanReason[];
}

export interface Preset {
  id: string;
  version: string;
  sha256: string;
  title: string;
  description?: string | null;
  input_sets: string[];
  targets: PlanTargets;
  implementations?: Record<string, { default?: string | null; fixed?: string | null }>;
  is_default: boolean;
  available: boolean;
  plans: PresetPlan[];
}

export interface PlanRequest {
  input_roles: string[];
  preset?: { id: string; version?: string | null } | null;
  targets?: PlanTargets | null;
  implementations?: Record<string, string>;
}

export interface Config {
  deployment_profile: string;
  run_plan_policy: 'presets_only' | 'free_for_admins' | 'free_for_researchers';
  free_mode_allowed: boolean;
}

export interface StepAttempt {
  id: string;
  run_slot_id: string;
  slot: string;
  attempt: number;
  trigger: 'initial' | 'auto_retry' | 'user_resume';
  status: RunStatus;
  outcome?: string | null;
  queued_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  failure_code?: string | null;
  failure_message?: string | null;
  log_available?: boolean;
}

export interface RunRead {
  id: string;
  case_id: string;
  pipeline_id: string;
  pipeline_version: string;
  input_set: string;
  status: RunStatus;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  failure_code?: string | null;
  failure_message?: string | null;
  cancel_requested_at?: string | null;
  resume_count?: number;
}

export interface RunDetail extends RunRead {
  cancel_requested?: boolean;
  plan?: RunPlan;
  inputs?: RunInput[];
  slots?: RunSlot[];
  steps?: StepAttempt[];
}

export interface Artifact {
  id: string;
  port: string;
  artefact_type: string;
  filename: string;
  size_bytes?: number | null;
  synthetic: boolean;
}

export interface Report {
  id: string;
  template: string;
  language: string;
  pdf_artifact_id: string;
  created_at: string;
}

export interface RunActionResult {
  run_id: string;
  status: string;
  changed?: boolean;
  cancel_requested?: boolean;
}

export interface RunCreate {
  pipeline_id: string;
  pipeline_version?: string | null;
  implementations?: Record<string, string>;
  input_roles?: string[] | null;
  /** A reviewed preset (exclusive with `targets`); without either the run gets the full plan. */
  preset?: { id: string; version?: string | null } | null;
  /** Free mode: the outputs wanted (exclusive with `preset`); 403 unless the deployment allows it. */
  targets?: PlanTargets | null;
}

/** Valid age bands (data-model section 9): 5-year bands 0-4 .. 85-89 plus the collapsed 90-104. */
export const AGE_BANDS: readonly string[] = [
  ...Array.from({ length: 18 }, (_, i) => `${i * 5}-${i * 5 + 4}`),
  '90-104',
];

/** Band containing a whole age in years, or null when the age is not a whole number in 0-104. */
export function ageToBand(age: number): string | null {
  if (!Number.isInteger(age) || age < 0 || age > 104) return null;
  if (age >= 90) return '90-104';
  const lo = Math.floor(age / 5) * 5;
  return `${lo}-${lo + 4}`;
}

const enc = encodeURIComponent;

export const listPipelines = (api: ApiClient) => api.json<PipelineSummary[]>('/v1/pipelines');

export const listSlots = (api: ApiClient, pipelineId: string, version: string) =>
  api.json<Slot[]>(`/v1/pipelines/${enc(pipelineId)}/versions/${enc(version)}/slots`);

export const getConfig = (api: ApiClient) => api.json<Config>('/v1/config');

export const listPresets = (api: ApiClient, pipelineId: string, version: string) =>
  api.json<Preset[]>(`/v1/pipelines/${enc(pipelineId)}/versions/${enc(version)}/presets`);

/** Dry run of run creation: a refused plan is a 200 with `feasible: false` and every reason. */
export const previewPlan = (api: ApiClient, pipelineId: string, version: string, body: PlanRequest) =>
  api.json<PlanPreview>(`/v1/pipelines/${enc(pipelineId)}/versions/${enc(version)}/plan`, {
    method: 'POST',
    json: body,
  });

export const listCases = (api: ApiClient) => api.json<CaseRead[]>('/v1/cases');

export const getCase = (api: ApiClient, caseId: string) => api.json<CaseDetail>(`/v1/cases/${enc(caseId)}`);

export const createCase = (api: ApiClient, body: CaseCreate) =>
  api.json<CaseDetail>('/v1/cases', { method: 'POST', json: body });

/** Upload the file of one input role. The file name is not used by the client beyond the upload itself. */
export const putInput = (api: ApiClient, caseId: string, role: string, file: File, origin?: InputOrigin | null) => {
  const form = new FormData();
  form.append('file', file);
  if (origin) form.append('origin', JSON.stringify(origin));
  return api.json<InputFile>(`/v1/cases/${enc(caseId)}/inputs/${enc(role)}`, { method: 'PUT', body: form });
};

export const createRun = (api: ApiClient, caseId: string, body: RunCreate) =>
  api.json<RunDetail>(`/v1/cases/${enc(caseId)}/runs`, { method: 'POST', json: body });

export const listCaseRuns = (api: ApiClient, caseId: string) => api.json<RunRead[]>(`/v1/cases/${enc(caseId)}/runs`);

export const getRun = (api: ApiClient, runId: string) => api.json<RunDetail>(`/v1/runs/${enc(runId)}`);

export const listArtifacts = (api: ApiClient, runId: string) => api.json<Artifact[]>(`/v1/runs/${enc(runId)}/artifacts`);

export const listReports = (api: ApiClient, runId: string) => api.json<Report[]>(`/v1/runs/${enc(runId)}/reports`);

export const downloadArtifact = (api: ApiClient, runId: string, artifactId: string) =>
  api.blob(`/v1/runs/${enc(runId)}/artifacts/${enc(artifactId)}/download`);

export const downloadReportPdf = (api: ApiClient, runId: string, reportId: string) =>
  api.blob(`/v1/runs/${enc(runId)}/reports/${enc(reportId)}/pdf`);

export interface StepLog {
  text: string;
  truncated: boolean;
}

/** Tail of one attempt log (plain text; `X-Log-Truncated: true` when the start is missing). */
export async function getStepLog(api: ApiClient, runId: string, stepRunId: string): Promise<StepLog> {
  const response = await api.request(`/v1/runs/${enc(runId)}/steps/${enc(stepRunId)}/log`);
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => undefined);
    throw new ApiError(response.status, payload);
  }
  return { text: await response.text(), truncated: response.headers.get('X-Log-Truncated') === 'true' };
}

export const cancelRun = (api: ApiClient, runId: string) =>
  api.json<RunActionResult>(`/v1/runs/${enc(runId)}/cancel`, { method: 'POST' });

export const resumeRun = (api: ApiClient, runId: string) =>
  api.json<RunActionResult>(`/v1/runs/${enc(runId)}/resume`, { method: 'POST' });

export const ACTIVE_STATUSES: readonly RunStatus[] = ['queued', 'running', 'awaiting_review'];
export const isActive = (status: RunStatus) => ACTIVE_STATUSES.includes(status);
