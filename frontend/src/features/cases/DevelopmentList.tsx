import { useTranslation } from 'react-i18next';
import type { DevelopmentStage } from '../../api/v1';

interface DevelopmentListProps {
  stages: readonly DevelopmentStage[];
  /** Short text under the heading (what "in development" means here); omitted when empty. */
  hint?: string;
  /** Heading text; defaults to "In development". */
  heading?: string;
}

/**
 * Stages of the process that are not available yet (the API's in-development list): greyed items
 * labelled "In development". They are plain list items, not form controls, so nothing here can be
 * chosen, requested or submitted. Titles and texts come from the catalogue by stage id (en, es);
 * the API's own text is the fallback for a stage the catalogue does not know.
 */
export function DevelopmentList({ stages, hint, heading }: DevelopmentListProps) {
  const { t, i18n } = useTranslation();
  if (stages.length === 0) return null;
  return (
    <section className="development" aria-label={heading ?? t('cases.development.heading')} data-testid="in-development">
      <h4>{heading ?? t('cases.development.heading')}</h4>
      {hint && <p className="hint">{hint}</p>}
      <ul className="development-list">
        {stages.map((stage) => {
          const textKey = `cases.development.stages.${stage.id}.text`;
          const text = i18n.exists(textKey) ? t(textKey) : (stage.description ?? '');
          return (
            <li key={stage.id} className="development-item" data-development={stage.id} aria-disabled="true">
              <strong>{t(`cases.development.stages.${stage.id}.title`, { defaultValue: stage.title })}</strong>
              <span className="tag tag-muted">{t('cases.development.badge')}</span>
              {text && <span className="hint block">{text}</span>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
