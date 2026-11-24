import type { InputOrigin } from '../../api/v1';

/**
 * Declared origin of a supplied input (contract 1.2, card F1-24): how the user says a mask was made.
 * The platform cannot verify it, so it is always presented as "declared by the user". Tool name and
 * version are restricted to a short token (no free text, which could carry identifiers), the same
 * pattern the API enforces.
 */
export type OriginKind = '' | 'manual' | 'external-tool';

export interface OriginDraft {
  kind: OriginKind;
  tool: string;
  version: string;
}

export const EMPTY_ORIGIN: OriginDraft = { kind: '', tool: '', version: '' };

export const ORIGIN_TOKEN = /^[A-Za-z0-9._+-]{1,64}$/;
export const ORIGIN_TOKEN_PATTERN = '[A-Za-z0-9._+-]{1,64}';

/** Roles that hold a lesion mask, the inputs a user can say were drawn by hand or made by a tool. */
export const originRoles = (roles: readonly string[]): string[] => roles.filter((role) => role.startsWith('lesion_'));

export function isValidOrigin(draft: OriginDraft): boolean {
  if (draft.kind === 'external-tool') return ORIGIN_TOKEN.test(draft.tool) && ORIGIN_TOKEN.test(draft.version);
  return true;
}

/** Reserved tool name the server gives to the synthetic example inputs it attaches. */
export const EXAMPLE_TOOL = 'stroke-server-example';

export const isExampleOrigin = (origin: InputOrigin | null | undefined): boolean =>
  origin?.kind === 'external-tool' && origin.tool === EXAMPLE_TOOL;

/** The origin to send, or null when none is declared. Call only with a valid draft. */
export function originOf(draft: OriginDraft | undefined): InputOrigin | null {
  if (!draft || draft.kind === '') return null;
  if (draft.kind === 'manual') return { kind: 'manual' };
  return { kind: 'external-tool', tool: draft.tool, version: draft.version };
}

/** Draft of an origin already stored (to edit it); an unknown kind reads as not declared. */
export function draftOf(origin: InputOrigin | null | undefined): OriginDraft {
  if (origin?.kind === 'manual') return { kind: 'manual', tool: '', version: '' };
  if (origin?.kind === 'external-tool') return { kind: 'external-tool', tool: origin.tool ?? '', version: origin.version ?? '' };
  return EMPTY_ORIGIN;
}
