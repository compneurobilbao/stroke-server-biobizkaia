import { useTranslation } from 'react-i18next';
import type { InputOrigin, RunPlan } from '../../api/v1';

/** Text of a declared input origin (contract 1.2): what the user said, not something the platform verified. */
type Translate = ReturnType<typeof useTranslation>['t'];

export function originText(t: Translate, origin: InputOrigin): string {
  if (origin.kind === 'manual') return t('cases.run.origin.manual');
  if (origin.kind === 'external-tool') {
    return t('cases.run.origin.externalTool', { tool: origin.tool ?? '', version: origin.version ?? '' }).trim();
  }
  return origin.kind;
}

/** The plan a run has: the preset (id and version), the custom targets, or the full plan. */
export function RunPlanSummary({ plan }: { plan: RunPlan | undefined }) {
  const { t } = useTranslation();
  if (plan?.mode === 'preset' && plan.preset) {
    return (
      <span data-plan-mode="preset">
        {t('cases.run.planPreset')} <strong>{plan.preset.id}</strong> <code>{plan.preset.version}</code>
      </span>
    );
  }
  if (plan?.mode === 'free') {
    const required = plan.targets?.required ?? [];
    const optional = plan.targets?.if_available ?? [];
    return (
      <span data-plan-mode="free">
        <strong>{t('cases.run.planCustom')}</strong>
        {required.length > 0 && (
          <span className="block">
            {t('cases.run.planRequired')}:{' '}
            {required.map((target) => (
              <code key={target} className="tag tag-muted">
                {target}
              </code>
            ))}
          </span>
        )}
        {optional.length > 0 && (
          <span className="block">
            {t('cases.run.planIfAvailable')}:{' '}
            {optional.map((target) => (
              <code key={target} className="tag tag-muted">
                {target}
              </code>
            ))}
          </span>
        )}
      </span>
    );
  }
  return <span data-plan-mode="full">{t('cases.run.planFull')}</span>;
}
