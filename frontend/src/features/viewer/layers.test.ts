import { describe, expect, it } from 'vitest';
import type { Artifact } from '../../api/v1';
import { niftiName, selectLayers } from './layers';

const artifact = (id: string, artefact_type: string): Artifact => ({
  id,
  port: artefact_type,
  artefact_type,
  filename: 'ignored.nii.gz',
  synthetic: false,
});

describe('selectLayers', () => {
  it('orders the layers background first and ignores other artefact types', () => {
    const layers = selectLayers([
      artifact('d', 'fdisconnection_map'),
      artifact('r', 'report_pdf'),
      artifact('l', 'lesion_mask_mni'),
      artifact('c', 'ct_mni'),
      artifact('q', 'qc_figure'),
    ]);
    expect(layers.map((layer) => [layer.kind, layer.artifact.id])).toEqual([
      ['template', 'c'],
      ['lesion', 'l'],
      ['disconnection', 'd'],
    ]);
  });

  it('has no background when the run has no ct_mni', () => {
    const layers = selectLayers([artifact('l', 'lesion_mask_mni')]);
    expect(layers.map((layer) => layer.kind)).toEqual(['lesion']);
  });

  it('takes the first artefact of a type', () => {
    const layers = selectLayers([artifact('a', 'lesion_mask_mni'), artifact('b', 'lesion_mask_mni')]);
    expect(layers).toHaveLength(1);
    expect(layers[0].artifact.id).toBe('a');
  });
});

describe('niftiName', () => {
  it('detects gzip data by its magic number', () => {
    expect(niftiName('lesion', new Uint8Array([0x1f, 0x8b, 8, 0]).buffer)).toBe('lesion.nii.gz');
  });

  it('treats anything else as an uncompressed NIfTI', () => {
    expect(niftiName('disconnection', new Uint8Array([92, 1, 0, 0]).buffer)).toBe('disconnection.nii');
    expect(niftiName('template', new ArrayBuffer(0))).toBe('template.nii');
  });
});
