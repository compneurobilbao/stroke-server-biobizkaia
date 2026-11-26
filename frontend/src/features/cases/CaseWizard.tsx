import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';
import { errorText } from '../../api/client';
import {
  AGE_BANDS,
  attachExampleInput,
  createCase,
  createRun,
  getConfig,
  listExampleInputs,
  listInDevelopment,
  listPipelines,
  listPresets,
  listSlots,
  previewPlan,
  putInput,
  type Config,
  type DevelopmentStage,
  type ExampleInput,
  type PipelineSummary,
  type Preset,
  type Sex,
  type Slot,
} from '../../api/v1';
import { useMessage } from '../../shared/messages';
import { useAuth } from '../auth/AuthContext';
import { AnalysisStep } from './AnalysisStep';
import { DevelopmentList } from './DevelopmentList';
import {
  CUSTOM_KEY,
  FULL_KEY,
  buildAnalysisOptions,
  buildPlanRequest,
  defaultAnalysisKey,
  inputSetName,
  isOfferedKey,
  planReasonsOf,
  presetKey,
  type CustomTargets,
  type RerunRequest,
  type TargetKind,
} from './plan';
import { IDLE_PREVIEW, ReasonList, slotTitleOf, type PreviewState } from './PlanPreview';
import { EMPTY_ORIGIN, isValidOrigin, originOf, originRoles, type OriginDraft } from './origin';
import { OriginField } from './OriginField';
import { SlotSelector } from './SlotSelector';
import {
  baselineImplementation,
  developmentBlocked,
  fixedImplementation,
  implementationOverrides,
  modeUnavailableSlots,
  slotLabel,
} from './slots';

export type WizardMode = 'ct' | 'mni';

/** Input roles of each mode (pipeline input names); the input set is inferred by the server from the roles. */
export const MODE_ROLES: Record<WizardMode, readonly string[]> = {
  ct: ['ct', 'lesion_native'],
  mni: ['lesion_mni'],
};

const STEPS = ['mode', 'files', 'analysis', 'steps'] as const;
type Step = (typeof STEPS)[number];
/** A new run on an existing case has its mode and files already: only the analysis and the implementations. */
const SAME_CASE_STEPS: readonly Step[] = ['analysis', 'steps'];

/** An existing case to run on again: its inputs are reused, never uploaded again. */
export interface SameCase {
  caseId: string;
  roles: string[];
}

/** Wait after the last change before the plan preview is requested. */
export const PREVIEW_DEBOUNCE_MS = 250;

function acceptsRoles(pipeline: PipelineSummary, roles: readonly string[]): boolean {
  return pipeline.input_sets.some(
    (set) => set.inputs.length === roles.length && roles.every((role) => set.inputs.includes(role)),
  );
}

function compareVersions(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true });
}


/** Analysis choice a recorded plan maps to (applied only when the deployment still offers it). */
function initialChoice(rerun: RerunRequest | null): string | null {
  if (!rerun) return null;
  if (rerun.planMode === 'preset' && rerun.presetId) return presetKey(rerun.presetId);
  return rerun.planMode === 'free' ? CUSTOM_KEY : FULL_KEY;
}

export function CaseWizard() {
  const { t } = useTranslation();
  const { api } = useAuth();
  const { setMessage } = useMessage();
  const navigate = useNavigate();
  // "Run again with the same plan" opens the wizard with the recorded request, on the same case
  // (`sameCase`: inputs reused) or on a new one.
  const locationState = useLocation().state as { rerun?: RerunRequest; sameCase?: SameCase } | null;
  const rerun = locationState?.rerun ?? null;
  const sameCase = locationState?.sameCase ?? null;
  const stepList: readonly Step[] = sameCase ? SAME_CASE_STEPS : STEPS;

  const [step, setStep] = useState<Step>(stepList[0]);
  const [mode, setMode] = useState<WizardMode>(rerun?.mode ?? 'ct');
  const [sex, setSex] = useState<'' | Sex>('');
  const [ageBand, setAgeBand] = useState('');
  const [files, setFiles] = useState<Record<string, File | null>>({});
  // How the user says each supplied mask was made (optional, declared and never verified).
  const [origins, setOrigins] = useState<Record<string, OriginDraft>>({});
  // Synthetic examples the deployment offers (empty unless it enables them) and the one chosen per role.
  const [examples, setExamples] = useState<ExampleInput[]>([]);
  const [exampleChoice, setExampleChoice] = useState<Record<string, ExampleInput | null>>({});
  const [pipelines, setPipelines] = useState<PipelineSummary[] | null>(null);
  const [slots, setSlots] = useState<Slot[] | null>(null);
  // Stages that are not available yet (never selectable); empty when the API lists none or fails.
  const [development, setDevelopment] = useState<DevelopmentStage[]>([]);
  const [config, setConfig] = useState<Config | null>(null);
  const [presets, setPresets] = useState<Preset[] | null>(null);
  // The user's explicit analysis choice (the default applies when it is not offered), the custom
  // outputs, and the implementations picked by hand (only those that differ from the default).
  const [analysisChoice, setAnalysisChoice] = useState<string | null>(initialChoice(rerun));
  const [custom, setCustom] = useState<CustomTargets>(rerun?.custom ?? {});
  const [picks, setPicks] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<PreviewState>(IDLE_PREVIEW);
  const [previewKey, setPreviewKey] = useState('');
  const [createError, setCreateError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  // Kept across attempts so that a retry after a failed upload or run creation reuses the case.
  const caseId = useRef<string | null>(sameCase?.caseId ?? null);
  const uploaded = useRef<Set<string>>(new Set(sameCase?.roles ?? []));
  const [locked, setLocked] = useState(false);

  const roles = sameCase ? sameCase.roles : MODE_ROLES[mode];
  const pipeline = useMemo(() => {
    const matching = (pipelines ?? []).filter((p) => acceptsRoles(p, roles));
    return matching.sort((a, b) => compareVersions(b.version, a.version))[0] ?? null;
  }, [pipelines, roles]);

  useEffect(() => {
    let cancelled = false;
    // Optional feature: a failure just means no examples are offered.
    listExampleInputs(api)
      .then((list) => {
        if (!cancelled) setExamples(list);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [api]);

  useEffect(() => {
    let cancelled = false;
    listPipelines(api)
      .then((list) => {
        if (!cancelled) setPipelines(list);
      })
      .catch((error) => {
        if (!cancelled) {
          setPipelines([]);
          setMessage(errorText(error, t('cases.wizard.pipelinesFailed')));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [api, setMessage, t]);

  // Whether the user may use free mode: the wizard offers "Custom" only when the server says so.
  useEffect(() => {
    let cancelled = false;
    getConfig(api)
      .then((value) => {
        if (!cancelled) setConfig(value);
      })
      .catch(() => {
        if (!cancelled) setConfig(null);
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

  useEffect(() => {
    if (!pipeline) {
      setSlots(null);
      setPresets(null);
      return;
    }
    let cancelled = false;
    setSlots(null);
    setPresets(null);
    setDevelopment([]);
    // Optional display: a failure just means no in-development stages are shown.
    listInDevelopment(api, pipeline.id, pipeline.version)
      .then((list) => {
        if (!cancelled) setDevelopment(list);
      })
      .catch(() => undefined);
    listSlots(api, pipeline.id, pipeline.version)
      .then((list) => {
        if (cancelled) return;
        setSlots(list);
        setPicks({});
      })
      .catch((error) => {
        if (!cancelled) setMessage(errorText(error, t('cases.wizard.slotsFailed')));
      });
    listPresets(api, pipeline.id, pipeline.version)
      .then((list) => {
        if (!cancelled) setPresets(list);
      })
      .catch((error) => {
        // Without the preset list the wizard offers the full plan, as before presets existed.
        if (cancelled) return;
        setPresets([]);
        setMessage(errorText(error, t('cases.wizard.presetsFailed')));
      });
    return () => {
      cancelled = true;
    };
  }, [api, pipeline, setMessage, t]);

  // A mode whose required slot is in development (CT: normalisation) is greyed and not selectable,
  // and so is one whose own required slot has no available implementation here (step not mounted).
  const ctInDevelopment = slots ? developmentBlocked(slots, development, MODE_ROLES.ct) : false;
  const ctUnavailable = slots && !ctInDevelopment ? modeUnavailableSlots(slots, MODE_ROLES.ct, MODE_ROLES.mni) : [];
  const ctBlocked = ctInDevelopment || ctUnavailable.length > 0;
  useEffect(() => {
    if (sameCase || !ctBlocked || mode !== 'ct') return;
    setMode('mni');
    setCustom({});
    setPicks({});
    setAnalysisChoice(null);
  }, [sameCase, ctBlocked, mode]);

  const customAllowed = config?.free_mode_allowed === true;
  const inputSet = pipeline ? inputSetName(pipeline, roles) : null;
  const options = useMemo(
    () => buildAnalysisOptions(presets ?? [], inputSet, customAllowed),
    [presets, inputSet, customAllowed],
  );
  const optionsReady = presets !== null;
  const selectedKey = optionsReady
    ? isOfferedKey(options, analysisChoice)
      ? analysisChoice
      : defaultAnalysisKey(options)
    : null;
  const selectedPreset = options.presets.find((option) => option.key === selectedKey)?.preset ?? null;
  const fixed = useMemo(() => {
    const result: Record<string, string> = {};
    for (const slot of slots ?? []) {
      const id = fixedImplementation(selectedPreset, slot.name);
      if (id) result[slot.name] = id;
    }
    return result;
  }, [slots, selectedPreset]);

  // The request of the preview and of the run creation: one body for both.
  const request = useMemo(
    () =>
      slots
        ? buildPlanRequest(
            selectedKey,
            options,
            custom,
            slots,
            roles,
            implementationOverrides(slots, picks, selectedPreset),
          )
        : null,
    [slots, selectedKey, options, custom, roles, picks, selectedPreset],
  );
  const requestKey = request ? JSON.stringify(request) : '';
  const stepIndex = stepList.indexOf(step);
  const previewActive = stepIndex >= stepList.indexOf('analysis');

  // Live plan preview: requested after every change, debounced; a newer change drops the older answer.
  useEffect(() => {
    setCreateError(null);
    if (!pipeline || !request || !previewActive) {
      setPreview(IDLE_PREVIEW);
      setPreviewKey('');
      return;
    }
    let cancelled = false;
    setPreview((previous) => ({ ...previous, status: 'loading', error: '' }));
    const timer = window.setTimeout(() => {
      previewPlan(api, pipeline.id, pipeline.version, request)
        .then((data) => {
          if (cancelled) return;
          setPreview({ status: 'ready', data, error: '' });
          setPreviewKey(requestKey);
        })
        .catch((error) => {
          if (cancelled) return;
          setPreview({ status: 'error', data: null, error: errorText(error, t('cases.plan.previewFailed')) });
          setPreviewKey('');
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // `request` is derived from the same inputs as `requestKey`, which makes this run once per change.
  }, [api, pipeline, requestKey, previewActive, t]);

  const missingFiles = roles.filter((role) => !files[role] && !exampleChoice[role] && !uploaded.current.has(role));
  const invalidOrigins = originRoles(roles).filter((role) => !isValidOrigin(origins[role] ?? EMPTY_ORIGIN));
  const feasible = preview.status === 'ready' && previewKey === requestKey && preview.data?.feasible === true;
  const canStart =
    !busy && pipeline !== null && slots !== null && request !== null && missingFiles.length === 0 && feasible;
  const titleOf = slotTitleOf(t, slots ?? []);

  function changeMode(value: WizardMode) {
    if (value === mode) return;
    setMode(value);
    // Slots and presets differ per mode: start the analysis choice again.
    setCustom({});
    setPicks({});
    setAnalysisChoice(null);
  }

  function changeCustom(slot: string, kind: TargetKind | null) {
    setCustom((previous) => {
      const next = { ...previous };
      if (kind) next[slot] = kind;
      else delete next[slot];
      return next;
    });
  }

  function changePick(slot: string, id: string) {
    const found = slots?.find((s) => s.name === slot);
    const baseline = found ? baselineImplementation(found, selectedPreset) : null;
    setPicks((previous) => {
      const next = { ...previous };
      if (id === baseline) delete next[slot];
      else next[slot] = id;
      return next;
    });
  }

  async function start() {
    if (!pipeline || !slots || !request) return;
    setBusy(true);
    setMessage('');
    setCreateError(null);
    try {
      let id = caseId.current;
      if (!id) {
        const created = await createCase(api, { sex: sex || null, age_band: ageBand || null });
        id = created.id;
        caseId.current = id;
        setLocked(true);
      }
      for (const role of roles) {
        const file = files[role];
        const example = exampleChoice[role];
        if (uploaded.current.has(role)) continue;
        if (example && !file) {
          await attachExampleInput(api, id, role, example.id);
        } else if (file) {
          await putInput(api, id, role, file, originOf(origins[role]));
        } else {
          continue;
        }
        uploaded.current.add(role);
      }
      const run = await createRun(api, id, {
        pipeline_id: pipeline.id,
        pipeline_version: pipeline.version,
        input_roles: [...roles],
        implementations: request.implementations ?? {},
        ...(request.preset ? { preset: request.preset } : {}),
        ...(request.targets ? { targets: request.targets } : {}),
      });
      navigate(`/runs/${run.id}`);
    } catch (error) {
      // The preview does not check bundles, manifests or parameters: the server can still refuse.
      setCreateError(error);
    } finally {
      setBusy(false);
    }
  }

  const createReasons = planReasonsOf(createError);
  // What the selectors show: the picks, else the implementation the plan resolved.
  const shown: Record<string, string> = {};
  for (const slot of preview.data?.slots ?? []) {
    if (slot.status === 'selected' && slot.implementation) shown[slot.name] = slot.implementation;
  }
  Object.assign(shown, picks);

  return (
    <section className="card wizard" aria-labelledby="wizard-title">
      <h2 id="wizard-title">{sameCase ? t('cases.wizard.titleAgain') : t('cases.wizard.title')}</h2>
      {sameCase ? (
        <p className="hint" data-testid="same-case-note">
          {t('cases.wizard.sameCase')}
        </p>
      ) : (
        <p className="hint">{t('cases.wizard.privacy')}</p>
      )}
      <ol className="wizard-steps" aria-label={t('cases.wizard.progress')}>
        {stepList.map((s, index) => (
          <li key={s} aria-current={s === step ? 'step' : undefined} className={s === step ? 'current' : ''}>
            {index + 1}. {t(`cases.wizard.step.${s}`)}
          </li>
        ))}
      </ol>

      {step === 'mode' && (
        <div className="wizard-panel">
          <fieldset className="wizard-fieldset" disabled={locked}>
            <legend>{t('cases.wizard.modeLegend')}</legend>
            {(['ct', 'mni'] as const).map((value) => {
              const inDevelopment = value === 'ct' && ctBlocked;
              return (
                <label key={value} className="radio-row" data-development={inDevelopment ? 'true' : undefined}>
                  <input
                    type="radio"
                    name="mode"
                    value={value}
                    checked={mode === value}
                    disabled={inDevelopment}
                    onChange={() => changeMode(value)}
                  />
                  <span>
                    <strong>{t(`cases.wizard.mode.${value}`)}</strong>
                    {inDevelopment && <span className="tag tag-muted">{t('cases.development.badge')}</span>}
                    <span className="hint block">{t(`cases.wizard.modeHint.${value}`)}</span>
                    {inDevelopment && (
                      <span className="hint block" data-testid={`mode-note-${value}`}>
                        {ctUnavailable.length > 0
                          ? t('cases.development.modeUnavailableNote', {
                              slots: ctUnavailable.map((slot) => slotLabel(t, slot.name, slot.title)).join(', '),
                            })
                          : t('cases.development.modeNote')}
                      </span>
                    )}
                  </span>
                </label>
              );
            })}
          </fieldset>
          <div className="field-row">
            <label>
              {t('cases.wizard.sex')}
              <select value={sex} disabled={locked} onChange={(event) => setSex(event.target.value as '' | Sex)}>
                <option value="">{t('cases.wizard.notSpecified')}</option>
                <option value="female">{t('cases.sex.female')}</option>
                <option value="male">{t('cases.sex.male')}</option>
              </select>
            </label>
            <label>
              {t('cases.wizard.ageBand')}
              <select value={ageBand} disabled={locked} onChange={(event) => setAgeBand(event.target.value)}>
                <option value="">{t('cases.wizard.notSpecified')}</option>
                {AGE_BANDS.map((band) => (
                  <option key={band} value={band}>
                    {t('cases.wizard.ageBandOption', { band })}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="hint">{t('cases.wizard.ageHint')}</p>
          {locked && <p className="hint">{t('cases.wizard.locked')}</p>}
        </div>
      )}

      {step === 'files' && (
        <div className="wizard-panel">
          <p className="hint">{t('cases.wizard.filesHint')}</p>
          {roles.map((role) => (
            <div key={role} className="file-group">
              <label className="file-row">
                <span>
                  <strong>{t(`cases.roles.${role}`, { defaultValue: role })}</strong>
                  {uploaded.current.has(role) && <span className="tag">{t('cases.wizard.uploaded')}</span>}
                </span>
                <input
                  key={exampleChoice[role] ? 'example' : 'file'}
                  type="file"
                  accept=".nii.gz"
                  aria-label={t(`cases.roles.${role}`, { defaultValue: role })}
                  onChange={(event) => {
                    const chosen = event.target.files?.[0] ?? null;
                    setFiles((prev) => ({ ...prev, [role]: chosen }));
                    if (chosen) setExampleChoice((prev) => ({ ...prev, [role]: null }));
                  }}
                />
              </label>
              {/* Only when the mode has a single input: a case never mixes examples and user files. */}
              {roles.length === 1 && !uploaded.current.has(role) && examples.some((e) => e.role === role) && (
                <div className="example-input" data-testid={`example-input-${role}`}>
                  {exampleChoice[role] ? (
                    <>
                      <span className="tag">{t('cases.wizard.example.selected', { label: exampleChoice[role]?.label })}</span>
                      <button
                        type="button"
                        className="secondary small"
                        onClick={() => setExampleChoice((prev) => ({ ...prev, [role]: null }))}
                      >
                        {t('cases.wizard.example.remove')}
                      </button>
                    </>
                  ) : (
                    examples
                      .filter((e) => e.role === role)
                      .map((example) => (
                        <button
                          key={example.id}
                          type="button"
                          className="secondary small"
                          title={example.description}
                          onClick={() => {
                            setExampleChoice((prev) => ({ ...prev, [role]: example }));
                            setFiles((prev) => ({ ...prev, [role]: null }));
                          }}
                        >
                          {examples.filter((e) => e.role === role).length > 1
                            ? t('cases.wizard.example.useNamed', { label: example.label })
                            : t('cases.wizard.example.use')}
                        </button>
                      ))
                  )}
                  <p className="hint">{t('cases.wizard.example.hint')}</p>
                </div>
              )}
              {originRoles([role]).length > 0 && !exampleChoice[role] && (
                <OriginField
                  role={role}
                  value={origins[role] ?? EMPTY_ORIGIN}
                  disabled={uploaded.current.has(role)}
                  onChange={(draft) => setOrigins((prev) => ({ ...prev, [role]: draft }))}
                />
              )}
            </div>
          ))}
        </div>
      )}

      {step === 'analysis' && (
        <>
          {pipelines && !pipeline && (
            <p className="hint" role="alert">
              {t('cases.wizard.noPipeline')}
            </p>
          )}
          {pipeline && (!optionsReady || !slots) && <p className="hint">{t('cases.wizard.loading')}</p>}
          {pipeline && optionsReady && slots && (
            <AnalysisStep
              options={options}
              selected={selectedKey}
              onSelect={setAnalysisChoice}
              slots={slots}
              roles={roles}
              custom={custom}
              onCustomChange={changeCustom}
              preview={preview}
              disabled={busy}
              development={development}
            />
          )}
        </>
      )}

      {step === 'steps' && (
        <div className="wizard-panel">
          <p className="hint">{t('cases.wizard.stepsHint')}</p>
          {pipelines && !pipeline && (
            <p className="hint" role="alert">
              {t('cases.wizard.noPipeline')}
            </p>
          )}
          {pipeline && (
            <p className="hint">
              {t('cases.wizard.pipeline')}: <strong>{pipeline.title}</strong> <code>{pipeline.version}</code>
              {selectedPreset && (
                <>
                  {' - '}
                  {t('cases.wizard.analysis')}: <strong>{selectedPreset.title}</strong>{' '}
                  <code>{selectedPreset.version}</code>
                </>
              )}
              {selectedKey === CUSTOM_KEY && (
                <>
                  {' - '}
                  {t('cases.wizard.analysis')}: <strong>{t('cases.analysis.custom')}</strong>
                </>
              )}
            </p>
          )}
          {pipeline && !preview.data && preview.status !== 'error' && (
            <p className="hint" role="status">
              {t('cases.wizard.loading')}
            </p>
          )}
          {pipeline && slots && preview.data && (
            <SlotSelector
              slots={slots}
              roles={roles}
              value={shown}
              plan={preview.data.slots ?? []}
              fixed={fixed}
              disabled={busy}
              onChange={changePick}
              development={development}
            />
          )}
          {pipeline && slots && preview.data && (
            <DevelopmentList stages={development} hint={t('cases.development.hint')} />
          )}
          {preview.status === 'error' && (
            <p className="hint" role="alert">
              {preview.error}
            </p>
          )}
          {preview.data && !preview.data.feasible && (
            <ReasonList
              reasons={preview.data.reasons ?? []}
              titleOf={titleOf}
              alert
              heading={t('cases.plan.refused')}
            />
          )}
        </div>
      )}

      {createError !== null && (
        <div className="create-error" data-testid="create-error">
          {createReasons.length > 0 ? (
            <ReasonList reasons={createReasons} titleOf={titleOf} alert heading={t('cases.wizard.refused')} />
          ) : (
            <p className="message" role="alert">
              {errorText(createError, t('cases.wizard.failed'))}
            </p>
          )}
        </div>
      )}

      <div className="wizard-actions">
        <button
          type="button"
          className="secondary"
          disabled={stepIndex === 0 || busy}
          onClick={() => setStep(stepList[stepIndex - 1])}
        >
          {t('cases.wizard.back')}
        </button>
        {step !== 'steps' && (
          <button
            type="button"
            disabled={
              (step === 'files' && (missingFiles.length > 0 || invalidOrigins.length > 0)) ||
              (step === 'analysis' && (!optionsReady || !slots || request === null))
            }
            onClick={() => setStep(stepList[stepIndex + 1])}
          >
            {t('cases.wizard.next')}
          </button>
        )}
        {step === 'steps' && (
          <button type="button" disabled={!canStart} onClick={() => void start()}>
            {busy ? t('cases.wizard.starting') : t('cases.wizard.start')}
          </button>
        )}
      </div>
    </section>
  );
}
