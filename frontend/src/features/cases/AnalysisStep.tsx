import { useTranslation } from 'react-i18next';
import type { DevelopmentStage, Slot } from '../../api/v1';
import {
  CUSTOM_KEY,
  FULL_KEY,
  type AnalysisOptions,
  type CustomTargets,
  type TargetKind,
} from './plan';
import { DevelopmentList } from './DevelopmentList';
import { PlanPreview, ReasonList, slotTitleOf, type PreviewState } from './PlanPreview';
import { developmentSlotNames, isSlotUnavailable, presetDescription, presetTitle, slotApplies, slotLabel } from './slots';

interface AnalysisStepProps {
  options: AnalysisOptions;
  /** Key of the chosen analysis (`preset:<id>`, `custom`, `full`) or null. */
  selected: string | null;
  onSelect: (key: string) => void;
  slots: readonly Slot[];
  roles: readonly string[];
  custom: CustomTargets;
  onCustomChange: (slot: string, kind: TargetKind | null) => void;
  preview: PreviewState;
  disabled?: boolean;
  /** Stages that are not available yet: listed greyed and never selectable. */
  development?: readonly DevelopmentStage[];
}

/**
 * The "Analysis" step: one of the enabled presets that accept the mode (unavailable ones are shown
 * disabled with their reasons), the full plan when no preset accepts it, and, only when the
 * deployment allows free mode to this user, a "Custom" option with a checklist of outputs. The
 * plan preview below follows every change. Stages in development are listed greyed under the choices.
 */
export function AnalysisStep({
  options,
  selected,
  onSelect,
  slots,
  roles,
  custom,
  onCustomChange,
  preview,
  disabled,
  development = [],
}: AnalysisStepProps) {
  const { t } = useTranslation();
  const titleOf = slotTitleOf(t, slots);
  const ordered = [...slots].sort((a, b) => a.position - b.position).filter((slot) => slotApplies(slot, roles));
  const noneRequested = !Object.values(custom).some(Boolean);
  const developmentSlots = developmentSlotNames(development);

  return (
    <div className="wizard-panel">
      <p className="hint">{t('cases.analysis.hint')}</p>
      <fieldset className="wizard-fieldset" disabled={disabled}>
        <legend>{t('cases.analysis.legend')}</legend>
        {options.presets.map((option) => (
          <label key={option.key} className="radio-row" data-analysis={option.key}>
            <input
              type="radio"
              name="analysis"
              value={option.key}
              checked={selected === option.key}
              disabled={!option.available}
              onChange={() => onSelect(option.key)}
            />
            <span>
              <strong>{presetTitle(t, option.preset)}</strong>
              <code className="tag tag-muted">{option.preset.version}</code>
              {option.preset.is_default && <span className="tag">{t('cases.analysis.recommended')}</span>}
              {!option.available && <span className="tag tag-warn">{t('cases.analysis.unavailable')}</span>}
              {option.preset.description && <span className="hint block">{presetDescription(t, option.preset)}</span>}
              {!option.available && (
                <>
                  {option.reasons.length === 0 && (
                    <span className="hint block">{t('cases.analysis.unavailableNoReason')}</span>
                  )}
                  <ReasonList reasons={option.reasons} titleOf={titleOf} />
                </>
              )}
            </span>
          </label>
        ))}
        {options.full && (
          <label className="radio-row" data-analysis={FULL_KEY}>
            <input
              type="radio"
              name="analysis"
              value={FULL_KEY}
              checked={selected === FULL_KEY}
              onChange={() => onSelect(FULL_KEY)}
            />
            <span>
              <strong>{t('cases.analysis.full')}</strong>
              <span className="hint block">{t('cases.analysis.fullHint')}</span>
            </span>
          </label>
        )}
        {options.custom && (
          <label className="radio-row" data-analysis={CUSTOM_KEY}>
            <input
              type="radio"
              name="analysis"
              value={CUSTOM_KEY}
              checked={selected === CUSTOM_KEY}
              onChange={() => onSelect(CUSTOM_KEY)}
            />
            <span>
              <strong>{t('cases.analysis.custom')}</strong>
              <span className="hint block">{t('cases.analysis.customHint')}</span>
            </span>
          </label>
        )}
      </fieldset>

      <DevelopmentList stages={development} hint={t('cases.development.hint')} />

      {options.custom && selected === CUSTOM_KEY && (
        <fieldset className="wizard-fieldset" disabled={disabled}>
          <legend>{t('cases.analysis.outputs')}</legend>
          <p className="hint">{t('cases.analysis.outputsHint')}</p>
          <ul className="slot-list">
            {ordered.map((slot) => {
              const title = slotLabel(t, slot.name, slot.title);
              const types = [...new Set((slot.produces ?? []).filter((p) => !p.optional).map((p) => p.type))];
              const inDevelopment = developmentSlots.has(slot.name);
              return (
                <li key={slot.name} className="slot-row" data-slot={slot.name} data-state={inDevelopment ? 'development' : 'ok'}>
                  <div className="slot-head">
                    <strong>{title}</strong>
                    {slot.optional && <span className="tag">{t('cases.slots.optional')}</span>}
                    {inDevelopment && <span className="tag tag-muted">{t('cases.development.badge')}</span>}
                    {!inDevelopment && isSlotUnavailable(slot) && (
                      <span className="tag tag-warn">{t('cases.slots.unavailable')}</span>
                    )}
                  </div>
                  {slot.description && <p className="hint slot-description">{slot.description}</p>}
                  {types.length > 0 && (
                    <p className="hint slot-description">
                      {t('cases.analysis.produces')}: {types.join(', ')}
                    </p>
                  )}
                  <select
                    aria-label={t('cases.analysis.request', { slot: title })}
                    value={custom[slot.name] ?? ''}
                    disabled={inDevelopment}
                    onChange={(event) => onCustomChange(slot.name, (event.target.value || null) as TargetKind | null)}
                  >
                    <option value="">{t('cases.analysis.notRequested')}</option>
                    <option value="required">{t('cases.analysis.required')}</option>
                    <option value="if_available">{t('cases.analysis.ifAvailable')}</option>
                  </select>
                </li>
              );
            })}
          </ul>
        </fieldset>
      )}

      <PlanPreview
        preview={preview}
        slots={slots}
        roles={roles}
        development={development}
        idleHint={
          selected === CUSTOM_KEY && noneRequested
            ? t('cases.analysis.chooseOutput')
            : options.presets.length > 0 && selected === null
              ? t('cases.analysis.chooseAnalysis')
              : undefined
        }
      />
    </div>
  );
}
