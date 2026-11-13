import { useTranslation } from 'react-i18next';

interface StatusProps {
  value: string;
  error?: string | null;
  warnings?: string | null;
}

export function Status({ value, error, warnings }: StatusProps) {
  const { t } = useTranslation();
  const label = t(`status.${value}`, { defaultValue: value });
  return (
    <span className={`status ${value}`} title={error || warnings || ''}>
      {label}
      {error ? `: ${error}` : ''}
      {!error && warnings ? ` ${t('status.withWarnings')}` : ''}
    </span>
  );
}
