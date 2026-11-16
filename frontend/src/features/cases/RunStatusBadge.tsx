import { useTranslation } from 'react-i18next';

/** Badge for a run, step attempt or slot state; `state` also drives the colour through `data-state`. */
export function RunStatusBadge({ state }: { state: string }) {
  const { t } = useTranslation();
  return (
    <span className="run-badge" data-state={state}>
      {t(`cases.state.${state}`, { defaultValue: state })}
    </span>
  );
}
