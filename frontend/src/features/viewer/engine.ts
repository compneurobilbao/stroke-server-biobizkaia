import type { LayerKind } from './layers';

/** One layer to load: the bytes are fetched by the API client; NiiVue never sees a URL or a token. */
export interface EngineLayerInput {
  kind: LayerKind;
  data: ArrayBuffer;
  name: string;
}

/** What the controls need to know about a loaded layer. */
export interface EngineLayerInfo {
  kind: LayerKind;
  /** Intensity range of the data (slider limits). */
  min: number;
  max: number;
  /** Lower display threshold NiiVue chose on load (values below it are transparent). */
  threshold: number;
  opacity: number;
}

/** What `ViewerPage` needs from a rendering engine; tests replace it because jsdom has no WebGL. */
export interface ViewerEngine {
  load(layers: EngineLayerInput[]): Promise<EngineLayerInfo[]>;
  setOpacity(kind: LayerKind, opacity: number): void;
  setThreshold(kind: LayerKind, threshold: number): void;
  destroy(): void;
}

export type EngineFactory = (canvas: HTMLCanvasElement) => Promise<ViewerEngine>;

interface LayerStyle {
  colormap: string;
  opacity: number;
}

/** Colour maps are NiiVue built-ins (compiled into the bundle), so nothing is fetched at run time. */
const STYLE: Record<LayerKind, LayerStyle> = {
  template: { colormap: 'gray', opacity: 1 },
  lesion: { colormap: 'blue', opacity: 0.8 },
  disconnection: { colormap: 'warm', opacity: 0.7 },
};

/**
 * NiiVue engine. The library is imported dynamically so that it only enters the chunk of the
 * lazily loaded viewer route, never the main bundle.
 */
export const createNiivueEngine: EngineFactory = async (canvas) => {
  const { Niivue, NVImage } = await import('@niivue/niivue');
  const nv = new Niivue({
    logLevel: 'error',
    backColor: [0, 0, 0, 1],
    isColorbar: true,
    dragAndDropEnabled: false,
    loadingText: '',
  });
  await nv.attachToCanvas(canvas);
  nv.setSliceType(nv.sliceTypeMultiplanar);

  const images = new Map<LayerKind, InstanceType<typeof NVImage>>();
  const initialMax = new Map<LayerKind, number>();

  return {
    async load(layers) {
      const infos: EngineLayerInfo[] = [];
      for (const layer of layers) {
        const style = STYLE[layer.kind];
        const volume = await NVImage.loadFromUrl({
          url: layer.data,
          name: layer.name,
          colormap: style.colormap,
          opacity: style.opacity,
        });
        nv.addVolume(volume);
        images.set(layer.kind, volume);
        const min = volume.global_min ?? 0;
        const max = volume.global_max ?? 1;
        initialMax.set(layer.kind, volume.cal_max ?? max);
        infos.push({
          kind: layer.kind,
          min,
          max,
          threshold: volume.cal_min ?? min,
          opacity: style.opacity,
        });
      }
      nv.updateGLVolume();
      return infos;
    },
    setOpacity(kind, opacity) {
      const volume = images.get(kind);
      if (!volume) return;
      nv.setOpacity(nv.volumes.indexOf(volume), opacity);
    },
    setThreshold(kind, threshold) {
      const volume = images.get(kind);
      if (!volume) return;
      volume.cal_min = threshold;
      // The upper limit stays where NiiVue put it unless the threshold would reach it.
      const upper = initialMax.get(kind) ?? threshold + 1;
      const span = (volume.global_max ?? upper) - (volume.global_min ?? 0) || 1;
      volume.cal_max = threshold < upper ? upper : threshold + span * 1e-3;
      nv.updateGLVolume();
    },
    destroy() {
      nv.cleanup();
      // Release the GPU context now; a new one is created when the page is opened again.
      canvas.getContext('webgl2')?.getExtension('WEBGL_lose_context')?.loseContext();
    },
  };
};
