export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

export function quotaBytesFromMbInput(value: string | null | undefined): number | null {
  if (value === '' || value == null) return null;
  const megabytes = Number(value);
  if (!Number.isFinite(megabytes) || megabytes < 0) return null;
  return Math.round(megabytes * 1024 * 1024);
}

export function sumValues(values: Record<string, number> | null | undefined): number {
  return Object.values(values || {}).reduce((total, value) => total + value, 0);
}

const DATE_LOCALES: Record<string, string> = { es: 'es-ES', en: 'en-GB' };

/** Date string from the API (UTC when no offset is given) shown in the Europe/Madrid zone. */
export function formatDateValue(value: string, language: string): string {
  const normalizedValue = /[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`;
  return new Date(normalizedValue).toLocaleString(DATE_LOCALES[language] ?? 'en-GB', { timeZone: 'Europe/Madrid' });
}
