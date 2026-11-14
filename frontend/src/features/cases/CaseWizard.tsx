import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { errorText } from '../../api/client';
import {
  AGE_BANDS,
  createCase,
  createRun,
  listPipelines,
  listSlots,
  putInput,
  type PipelineSummary,
  type Sex,
  type Slot,
} from '../../api/v1';
import { useMessage } from '../../shared/messages';
import { useAuth } from '../auth/AuthContext';
import { SlotSelector } from './SlotSelector';
import { blockingSlots, defaultChoices, overrides } from './slots';

export type WizardMode = 'ct' | 'mni';

/** Input roles of each mode (pipeline input names); the input set is inferred by the server from the roles. */
export const MODE_ROLES: Record<WizardMode, readonly string[]> = {
  ct: ['ct', 'lesion_native'],
  mni: ['lesion_mni'],
};

const STEPS = ['mode', 'files', 'steps'] as const;
type Step = (typeof STEPS)[number];

function acceptsRoles(pipeline: PipelineSummary, roles: readonly string[]): boolean {
  return pipeline.input_sets.some(
    (set) => set.inputs.length === roles.length && roles.every((role) => set.inputs.includes(role)),
  );
}

function compareVersions(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true });
}

export function CaseWizard() {
  const { t } = useTranslation();
  const { api } = useAuth();
  const { setMessage } = useMessage();
  const navigate = useNavigate();

  const [step, setStep] = useState<Step>('mode');
  const [mode, setMode] = useState<WizardMode>('ct');
  const [sex, setSex] = useState<'' | Sex>('');
  const [ageBand, setAgeBand] = useState('');
  const [files, setFiles] = useState<Record<string, File | null>>({});
  const [pipelines, setPipelines] = useState<PipelineSummary[] | null>(null);
  const [slots, setSlots] = useState<Slot[] | null>(null);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  // Kept across attempts so that a retry after a failed upload or run creation reuses the case.
  const caseId = useRef<string | null>(null);
  const uploaded = useRef<Set<string>>(new Set());
  const [locked, setLocked] = useState(false);

  const roles = MODE_ROLES[mode];
  const pipeline = useMemo(() => {
    const matching = (pipelines ?? []).filter((p) => acceptsRoles(p, roles));
    return matching.sort((a, b) => compareVersions(b.version, a.version))[0] ?? null;
  }, [pipelines, roles]);

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

  useEffect(() => {
    if (!pipeline) {
      setSlots(null);
      return;
    }
    let cancelled = false;
    setSlots(null);
    listSlots(api, pipeline.id, pipeline.version)
      .then((list) => {
        if (cancelled) return;
        setSlots(list);
        setChoices(defaultChoices(list));
      })
      .catch((error) => {
        if (!cancelled) setMessage(errorText(error, t('cases.wizard.slotsFailed')));
      });
    return () => {
      cancelled = true;
    };
  }, [api, pipeline, setMessage, t]);

  const missingFiles = roles.filter((role) => !files[role] && !uploaded.current.has(role));
  const blocking = slots ? blockingSlots(slots, roles) : [];
  const canStart = !busy && pipeline !== null && slots !== null && missingFiles.length === 0 && blocking.length === 0;

  async function start() {
    if (!pipeline || !slots) return;
    setBusy(true);
    setMessage('');
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
        if (uploaded.current.has(role) || !file) continue;
        await putInput(api, id, role, file);
        uploaded.current.add(role);
      }
      const run = await createRun(api, id, {
        pipeline_id: pipeline.id,
        pipeline_version: pipeline.version,
        input_roles: [...roles],
        implementations: overrides(slots, choices),
      });
      navigate(`/runs/${run.id}`);
    } catch (error) {
      setMessage(errorText(error, t('cases.wizard.failed')));
    } finally {
      setBusy(false);
    }
  }

  const stepIndex = STEPS.indexOf(step);

  return (
    <section className="card wizard" aria-labelledby="wizard-title">
      <h2 id="wizard-title">{t('cases.wizard.title')}</h2>
      <p className="hint">{t('cases.wizard.privacy')}</p>
      <ol className="wizard-steps" aria-label={t('cases.wizard.progress')}>
        {STEPS.map((s, index) => (
          <li key={s} aria-current={s === step ? 'step' : undefined} className={s === step ? 'current' : ''}>
            {index + 1}. {t(`cases.wizard.step.${s}`)}
          </li>
        ))}
      </ol>

      {step === 'mode' && (
        <div className="wizard-panel">
          <fieldset className="wizard-fieldset" disabled={locked}>
            <legend>{t('cases.wizard.modeLegend')}</legend>
            {(['ct', 'mni'] as const).map((value) => (
              <label key={value} className="radio-row">
                <input
                  type="radio"
                  name="mode"
                  value={value}
                  checked={mode === value}
                  onChange={() => setMode(value)}
                />
                <span>
                  <strong>{t(`cases.wizard.mode.${value}`)}</strong>
                  <span className="hint block">{t(`cases.wizard.modeHint.${value}`)}</span>
                </span>
              </label>
            ))}
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
            <label key={role} className="file-row">
              <span>
                <strong>{t(`cases.roles.${role}`, { defaultValue: role })}</strong>
                {uploaded.current.has(role) && <span className="tag">{t('cases.wizard.uploaded')}</span>}
              </span>
              <input
                type="file"
                accept=".nii.gz"
                aria-label={t(`cases.roles.${role}`, { defaultValue: role })}
                onChange={(event) => setFiles((prev) => ({ ...prev, [role]: event.target.files?.[0] ?? null }))}
              />
            </label>
          ))}
        </div>
      )}

      {step === 'steps' && (
        <div className="wizard-panel">
          <p className="hint">{t('cases.wizard.stepsHint')}</p>
          {pipelines && !pipeline && <p className="hint" role="alert">{t('cases.wizard.noPipeline')}</p>}
          {pipeline && (
            <p className="hint">
              {t('cases.wizard.pipeline')}: <strong>{pipeline.title}</strong> <code>{pipeline.version}</code>
            </p>
          )}
          {pipeline && !slots && <p className="hint">{t('cases.wizard.loading')}</p>}
          {slots && (
            <SlotSelector
              slots={slots}
              roles={roles}
              value={choices}
              disabled={busy}
              onChange={(slot, id) => setChoices((prev) => ({ ...prev, [slot]: id }))}
            />
          )}
          {blocking.length > 0 && (
            <p className="hint" role="alert">
              {t('cases.wizard.blocked', { slots: blocking.map((s) => s.title || s.name).join(', ') })}
            </p>
          )}
        </div>
      )}

      <div className="wizard-actions">
        <button
          type="button"
          className="secondary"
          disabled={stepIndex === 0 || busy}
          onClick={() => setStep(STEPS[stepIndex - 1])}
        >
          {t('cases.wizard.back')}
        </button>
        {step !== 'steps' && (
          <button
            type="button"
            disabled={step === 'files' && missingFiles.length > 0}
            onClick={() => setStep(STEPS[stepIndex + 1])}
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
