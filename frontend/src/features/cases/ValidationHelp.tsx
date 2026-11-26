import { useTranslation } from 'react-i18next';

/**
 * What the validation levels mean (reference-tested vs validated) and that results are for research
 * use only; shown wherever an implementation's validation level or "experimental" tag appears.
 */
export function ValidationHelp() {
  const { t } = useTranslation();
  return (
    <p className="hint validation-help" data-testid="validation-help">
      {t('cases.validation.help')}
    </p>
  );
}
