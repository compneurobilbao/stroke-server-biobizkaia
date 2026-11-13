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
});
