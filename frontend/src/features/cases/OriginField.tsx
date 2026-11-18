import { useTranslation } from 'react-i18next';
import { EMPTY_ORIGIN, ORIGIN_TOKEN, ORIGIN_TOKEN_PATTERN, type OriginDraft, type OriginKind } from './origin';

interface Props {
  role: string;
  value: OriginDraft;
  disabled?: boolean;
  onChange: (value: OriginDraft) => void;
}

/**
 * Optional declaration of how a supplied mask was made (card F1-24). The platform cannot verify
 * it: the text says so, and the run page and the report show it as declared by the user.
 */
export function OriginField({ role, value, disabled = false, onChange }: Props) {
  const { t } = useTranslation();
  const external = value.kind === 'external-tool';
  const toolBad = external && value.tool !== '' && !ORIGIN_TOKEN.test(value.tool);
  const versionBad = external && value.version !== '' && !ORIGIN_TOKEN.test(value.version);
  const legend = t('cases.wizard.origin.legend');
  return (
    <fieldset className="origin-field" data-testid={`origin-field-${role}`} disabled={disabled}>
      <legend>{legend}</legend>
      <div className="field-row">
        <label>
          <span>{t('cases.wizard.origin.kind')}</span>
          <select
            value={value.kind}
            onChange={(event) => onChange({ ...EMPTY_ORIGIN, ...value, kind: event.target.value as OriginKind })}
          >
            <option value="">{t('cases.wizard.origin.none')}</option>
            <option value="manual">{t('cases.wizard.origin.manual')}</option>
            <option value="external-tool">{t('cases.wizard.origin.externalTool')}</option>
          </select>
        </label>
      </div>
      {external && (
        <>
          <div className="field-row">
            <label>
              <span>{t('cases.wizard.origin.tool')}</span>
              <input
                type="text"
                value={value.tool}
                maxLength={64}
                pattern={ORIGIN_TOKEN_PATTERN}
                autoComplete="off"
                aria-invalid={toolBad || value.tool === ''}
                onChange={(event) => onChange({ ...value, tool: event.target.value })}
              />
            </label>
            <label>
              <span>{t('cases.wizard.origin.version')}</span>
              <input
                type="text"
                value={value.version}
                maxLength={64}
                pattern={ORIGIN_TOKEN_PATTERN}
                autoComplete="off"
                aria-invalid={versionBad || value.version === ''}
                onChange={(event) => onChange({ ...value, version: event.target.value })}
              />
            </label>
          </div>
          {(toolBad || versionBad) && (
            <p className="hint" role="alert">
              {t('cases.wizard.origin.invalid')}
            </p>
          )}
          {!toolBad && !versionBad && (value.tool === '' || value.version === '') && (
            <p className="hint">{t('cases.wizard.origin.needBoth')}</p>
          )}
        </>
      )}
      <p className="hint">{t('cases.wizard.origin.hint')}</p>
    </fieldset>
  );
}
