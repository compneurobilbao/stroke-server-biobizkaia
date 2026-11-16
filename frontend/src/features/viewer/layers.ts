import type { Artifact } from '../../api/v1';

/** The three layers of the read-only viewer, in drawing order (background first). */
export type LayerKind = 'template' | 'lesion' | 'disconnection';

/** Artefact type (contracts/artefact-types.yaml) each layer is taken from. */
export const LAYER_ARTEFACT_TYPE: Record<LayerKind, string> = {
  template: 'ct_mni',
  lesion: 'lesion_mask_mni',
  disconnection: 'fdisconnection_map',
};

export const LAYER_ORDER: readonly LayerKind[] = ['template', 'lesion', 'disconnection'];

export interface LayerSpec {
  kind: LayerKind;
  artifact: Artifact;
}

/**
 * Layers the viewer can show for a run: the first artefact of each supported type.
 * The background is the run's own `ct_mni` when the normalisation produced one; no template
 * is bundled with the application (see README.md in this folder), so otherwise there is no background.
 */
export function selectLayers(artifacts: Artifact[]): LayerSpec[] {
  const specs: LayerSpec[] = [];
  for (const kind of LAYER_ORDER) {
    const artifact = artifacts.find((a) => a.artefact_type === LAYER_ARTEFACT_TYPE[kind]);
    if (artifact) specs.push({ kind, artifact });
  }
  return specs;
}

/** NiiVue infers the format from the file name; name the in-memory bytes by their gzip magic number. */
export function niftiName(kind: LayerKind, data: ArrayBuffer): string {
  const bytes = new Uint8Array(data, 0, Math.min(2, data.byteLength));
  const gzipped = bytes.length === 2 && bytes[0] === 0x1f && bytes[1] === 0x8b;
  return `${kind}${gzipped ? '.nii.gz' : '.nii'}`;
}
