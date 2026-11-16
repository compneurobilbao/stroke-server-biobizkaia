import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router-dom';
import { errorText } from '../../api/client';
import { downloadArtifact, listArtifacts } from '../../api/v1';
import { useAuth } from '../auth/AuthContext';
import { createNiivueEngine, type EngineLayerInfo, type ViewerEngine } from './engine';
import { niftiName, selectLayers, type LayerKind, type LayerSpec } from './layers';

type Status = 'loading' | 'ready' | 'empty' | 'error';

interface LayerState extends EngineLayerInfo {
  visible: boolean;
  synthetic: boolean;
}

/** Overlays have a display threshold; the background does not. */
const HAS_THRESHOLD: Record<LayerKind, boolean> = { template: false, lesion: true, disconnection: true };

/** Slider step for a data range: fine enough for 0-1 masks and for map statistics alike. */
function stepFor(min: number, max: number): number {
  const span = max - min;
  if (!(span > 0)) return 0.01;
  return Number((span / 200).toPrecision(1));
}

/**
 * Read-only NiiVue view of a finished run (card F0-32): the case CT in MNI space as background
 * when the run produced one, the lesion mask and the disconnection map as overlays. The image
 * bytes are fetched with the API client (bearer token in the header) and handed to NiiVue as
 * in-memory buffers; no URL carries a token and nothing is loaded from a CDN.
 */
export function ViewerPage() {
  const { runId = '' } = useParams();
  const { t } = useTranslation();
  const { api } = useAuth();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<ViewerEngine | null>(null);
  const [status, setStatus] = useState<Status>('loading');
  const [error, setError] = useState('');
  const [layers, setLayers] = useState<LayerState[]>([]);

  useEffect(() => {
    let cancelled = false;
    let engine: ViewerEngine | null = null;

    async function start() {
      try {
        const specs: LayerSpec[] = selectLayers(await listArtifacts(api, runId));
        if (!specs.some((spec) => spec.kind !== 'template')) {
          if (!cancelled) setStatus('empty');
          return;
        }
        const inputs = await Promise.all(
          specs.map(async (spec) => {
            const data = await (await downloadArtifact(api, runId, spec.artifact.id)).arrayBuffer();
            return { kind: spec.kind, data, name: niftiName(spec.kind, data) };
          }),
        );
        const canvas = canvasRef.current;
        if (cancelled || !canvas) return;
        engine = await createNiivueEngine(canvas);
        if (cancelled) return;
        const infos = await engine.load(inputs);
        if (cancelled) return;
        engineRef.current = engine;
        setLayers(
          infos.map((info) => ({
            ...info,
            visible: true,
            synthetic: specs.find((spec) => spec.kind === info.kind)?.artifact.synthetic ?? false,
          })),
        );
        setStatus('ready');
      } catch (failure) {
        if (cancelled) return;
        setError(errorText(failure, t('viewer.loadFailed')));
        setStatus('error');
      }
    }

    void start();
    return () => {
      cancelled = true;
      engineRef.current = null;
      engine?.destroy();
    };
  }, [api, runId, t]);

  function update(kind: LayerKind, patch: Partial<Pick<LayerState, 'visible' | 'opacity' | 'threshold'>>) {
    const engine = engineRef.current;
    setLayers((current) =>
      current.map((layer) => (layer.kind === kind ? { ...layer, ...patch } : layer)),
    );
    const layer = layers.find((candidate) => candidate.kind === kind);
    if (!layer || !engine) return;
    const next = { ...layer, ...patch };
    if (patch.visible !== undefined || patch.opacity !== undefined) {
      engine.setOpacity(kind, next.visible ? next.opacity : 0);
    }
    if (patch.threshold !== undefined) engine.setThreshold(kind, next.threshold);
  }

  const hasTemplate = layers.some((layer) => layer.kind === 'template');

  return (
    <section className="card" aria-labelledby="viewer-title">
      <div className="section-title">
        <h2 id="viewer-title">{t('viewer.title')}</h2>
        <Link className="download secondary" to={`/runs/${runId}`}>
          {t('viewer.backToRun')}
        </Link>
      </div>
      <p className="hint">{t('viewer.researchOnly')}</p>
      {status === 'loading' && <p className="hint">{t('viewer.loading')}</p>}
      {status === 'empty' && <p className="hint">{t('viewer.empty')}</p>}
      {status === 'error' && (
        <p className="message" role="alert">
          {error}
        </p>
      )}
      <div
        className="viewer-layout"
        data-state={status}
        data-layers={layers.map((layer) => layer.kind).join(',')}
        hidden={status === 'empty'}
      >
        <div className="viewer-canvas-wrap">
          <canvas ref={canvasRef} className="viewer-canvas" role="img" aria-label={t('viewer.canvasLabel')} />
        </div>
        {status === 'ready' && (
          <div className="viewer-controls">
            {layers.map((layer) => (
              <fieldset key={layer.kind} className="wizard-fieldset viewer-layer" data-layer={layer.kind}>
                <legend>
                  {t(`viewer.layer.${layer.kind}`)}
                  {layer.synthetic && <span className="tag tag-warn">{t('cases.run.synthetic')}</span>}
                </legend>
                <label className="viewer-check">
                  <input
                    type="checkbox"
                    checked={layer.visible}
                    onChange={(event) => update(layer.kind, { visible: event.target.checked })}
                  />
                  {t('viewer.show')}
                </label>
                <label className="viewer-slider">
                  <span>{t('viewer.opacity')}</span>
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={layer.opacity}
                    aria-label={t('viewer.opacityOf', { layer: t(`viewer.layer.${layer.kind}`) })}
                    onChange={(event) => update(layer.kind, { opacity: Number(event.target.value) })}
                  />
                  <output>{layer.opacity.toFixed(2)}</output>
                </label>
                {HAS_THRESHOLD[layer.kind] && (
                  <label className="viewer-slider">
                    <span>{t('viewer.threshold')}</span>
                    <input
                      type="range"
                      min={layer.min}
                      max={layer.max}
                      step={stepFor(layer.min, layer.max)}
                      value={layer.threshold}
                      aria-label={t('viewer.thresholdOf', { layer: t(`viewer.layer.${layer.kind}`) })}
                      onChange={(event) => update(layer.kind, { threshold: Number(event.target.value) })}
                    />
                    <output>{Number(layer.threshold.toPrecision(3))}</output>
                  </label>
                )}
              </fieldset>
            ))}
            {!hasTemplate && (
              <p className="hint" role="note" data-testid="no-template">
                {t('viewer.noTemplate')}
              </p>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
