import { describe, expect, it } from 'vitest';
import en from './locales/en.json';
import es from './locales/es.json';

function flatten(value: unknown, prefix = ''): string[] {
  if (value !== null && typeof value === 'object') {
    return Object.entries(value).flatMap(([key, child]) => flatten(child, prefix ? `${prefix}.${key}` : key));
  }
  return [prefix];
}

describe('translation catalogues', () => {
  it('define the same keys in en and es', () => {
    expect(flatten(es).sort()).toEqual(flatten(en).sort());
  });

  it('use formal "usted" and "TAC" for CT in Spanish', () => {
    const values = (function collect(value: unknown): string[] {
      if (value !== null && typeof value === 'object') return Object.values(value).flatMap(collect);
      return [String(value)];
    })(es);
    // Informal (tú) forms that the catalogue used before; any of them means a string slipped back.
    const informal = /\b(quieres|tienes|puedes|te ha|Sube|Inicia sesión|Vuelve a|Selecciona|Usa|Indica|Deja|Cópialo|escribas)\b/;
    for (const text of values) {
      expect(text, text).not.toMatch(informal);
      expect(text, text).not.toMatch(/\bTC\b/);
    }
  });
});
