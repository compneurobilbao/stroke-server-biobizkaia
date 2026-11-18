import { describe, expect, it } from 'vitest';
import { EMPTY_ORIGIN, draftOf, isValidOrigin, originOf, originRoles } from './origin';

describe('declared origin', () => {
  it('declares nothing by default', () => {
    expect(originOf(EMPTY_ORIGIN)).toBeNull();
    expect(originOf(undefined)).toBeNull();
    expect(isValidOrigin(EMPTY_ORIGIN)).toBe(true);
  });

  it('manual carries no other field, even if the draft still holds a tool', () => {
    expect(originOf({ kind: 'manual', tool: 'x', version: '1' })).toEqual({ kind: 'manual' });
  });

  it('external tool needs a name and a version made of the allowed characters only', () => {
    const ok = { kind: 'external-tool' as const, tool: 'example-tool', version: '2.1.0+build_3' };
    expect(isValidOrigin(ok)).toBe(true);
    expect(originOf(ok)).toEqual({ kind: 'external-tool', tool: 'example-tool', version: '2.1.0+build_3' });
    for (const [tool, version] of [
      ['', '1'],
      ['x', ''],
      ['two words', '1'],
      ['a@b.org', '1'],
      ['a/b', '1'],
      ['x', '1\n'],
      ['x', 'v'.repeat(65)],
    ]) {
      expect(isValidOrigin({ kind: 'external-tool', tool, version })).toBe(false);
    }
    expect(isValidOrigin({ kind: 'external-tool', tool: 'a'.repeat(64), version: '1' })).toBe(true);
  });

  it('only lesion masks can have a declared origin', () => {
    expect(originRoles(['ct', 'lesion_native'])).toEqual(['lesion_native']);
    expect(originRoles(['lesion_mni'])).toEqual(['lesion_mni']);
  });
});

describe('editing a stored origin', () => {
  it('turns a stored origin into the draft of the form, and anything else into "not declared"', () => {
    expect(draftOf({ kind: 'manual' })).toEqual({ kind: 'manual', tool: '', version: '' });
    expect(draftOf({ kind: 'external-tool', tool: 'example-tool', version: '2.1' })).toEqual({
      kind: 'external-tool',
      tool: 'example-tool',
      version: '2.1',
    });
    expect(draftOf(null)).toEqual(EMPTY_ORIGIN);
    expect(draftOf(undefined)).toEqual(EMPTY_ORIGIN);
    expect(draftOf({ kind: 'platform-run' })).toEqual(EMPTY_ORIGIN);
  });

  it('round-trips through the value that is sent', () => {
    const origin = { kind: 'external-tool', tool: 'example-tool', version: '2.1' };
    expect(originOf(draftOf(origin))).toEqual(origin);
    expect(originOf(draftOf(null))).toBeNull();
  });
});
