import { useTranslation } from 'react-i18next';

interface Stage {
  key: string;
  tool?: boolean;
}

const BRANCH_FUNCTIONAL: Stage[] = [
  { key: 'flnm', tool: true },
  { key: 'disconnectionMap' },
  { key: 'cognet', tool: true },
];

const BRANCH_STRUCTURAL: Stage[] = [{ key: 'tracts', tool: true }, { key: 'tractTable' }];

const PLANNED: string[] = ['normalisation', 'segmentation', 'lesionBank', 'voxelLnm'];

function Arrow() {
  return (
    <span className="process-arrow" aria-hidden="true">
      ↓
    </span>
  );
}

function Branch({ id, stages }: { id: string; stages: Stage[] }) {
  const { t } = useTranslation();
  return (
    <li className="process-branch">
      <h4>{t(`process.branch.${id}`)}</h4>
      <ol className="process-chain">
        {stages.map((stage, index) => (
          <li key={stage.key}>
            {index > 0 && <Arrow />}
            <div className={stage.tool ? 'process-node process-node-tool' : 'process-node'}>
              <strong>{t(`process.stage.${stage.key}.title`)}</strong>
              <span>{t(`process.stage.${stage.key}.text`)}</span>
            </div>
          </li>
        ))}
      </ol>
    </li>
  );
}

/** Whole-process diagram: what runs today and what is still in development (HTML/CSS only, no assets). */
export function ProcessOverview({ id = 'process' }: { id?: string }) {
  const { t } = useTranslation();
  return (
    <section className="process" id={id} aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`}>{t('process.title')}</h2>
      <p className="process-intro">{t('process.intro')}</p>

      <div className="process-available">
        <h3>{t('process.availableTitle')}</h3>
        <div className="process-node process-node-input">
          <strong>{t('process.stage.input.title')}</strong>
          <span>{t('process.stage.input.text')}</span>
        </div>
        <Arrow />
        <ul className="process-branches">
          <Branch id="functional" stages={BRANCH_FUNCTIONAL} />
          <Branch id="structural" stages={BRANCH_STRUCTURAL} />
        </ul>
        <Arrow />
        <div className="process-node process-node-output">
          <strong>{t('process.stage.report.title')}</strong>
          <span>{t('process.stage.report.text')}</span>
        </div>
      </div>

      <div className="process-planned">
        <h3>{t('process.plannedTitle')}</h3>
        <p className="process-planned-note">{t('process.plannedNote')}</p>
        <ul className="process-planned-list">
          {PLANNED.map((key) => (
            <li key={key} className="process-node process-node-planned" aria-disabled="true">
              <span className="process-dev-badge">{t('process.inDevelopment')}</span>
              <strong>{t(`process.planned.${key}.title`)}</strong>
              <span>{t(`process.planned.${key}.text`)}</span>
            </li>
          ))}
        </ul>
      </div>
      <p className="process-note">{t('process.researchNote')}</p>
    </section>
  );
}

/** Page behind the "About the process" link; reachable before and after login. */
export function ProcessPage() {
  const { t } = useTranslation();
  return (
    <div className="process-page">
      <h1>{t('process.pageTitle')}</h1>
      <ProcessOverview id="process-page" />
    </div>
  );
}
