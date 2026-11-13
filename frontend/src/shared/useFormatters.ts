import { useTranslation } from 'react-i18next';
import { formatBytes, formatDateValue } from './format';

export function useFormatters() {
  const { t, i18n } = useTranslation();
  return {
    date: (value: string | null | undefined): string =>
      value ? formatDateValue(value, i18n.language) : t('common.notInformed'),
    quota: (bytes: number | null | undefined): string => (bytes == null ? t('common.unlimited') : formatBytes(bytes)),
  };
}
