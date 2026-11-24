import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { errorText } from '../../api/client';
import { patchInputOrigin, type InputFile } from '../../api/v1';
import { useAuth } from '../auth/AuthContext';
import { draftOf, isExampleOrigin, isValidOrigin, originOf, originRoles, type OriginDraft } from './origin';
import { OriginField } from './OriginField';
import { originText } from './RunPlanSummary';

interface Props {
  caseId: string;
  input: InputFile;
  /** The owner may change the declaration, and only while the case has no run (the API refuses it afterwards). */
  editable: boolean;
  onSaved: (input: InputFile) => void;
}

/** Declared origin of one input on the case page, with an edit form for the owner before the first run. */
export function InputOriginEditor({ caseId, input, editable, onSaved }: Props) {
  const { t } = useTranslation();
  const { api } = useAuth();
  const [draft, setDraft] = useState<OriginDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const canEdit = editable && originRoles([input.role]).length > 0 && !isExampleOrigin(input.origin);

  async function save() {
    if (!draft) return;
    setBusy(true);
    setError('');
    try {
      onSaved(await patchInputOrigin(api, caseId, input.role, originOf(draft)));
      setDraft(null);
    } catch (e) {
      setError(errorText(e, t('cases.detail.originFailed')));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {input.origin && (
        <span className="hint block" data-testid={`origin-${input.role}`}>
          {t('cases.run.declaredByUser')}: {originText(t, input.origin)}
        </span>
      )}
      {canEdit && draft === null && (
        <button type="button" className="secondary small" onClick={() => setDraft(draftOf(input.origin))}>
          {t('cases.detail.editOrigin')}
        </button>
      )}
      {canEdit && draft !== null && (
        <div className="origin-edit">
          <OriginField role={input.role} value={draft} disabled={busy} onChange={setDraft} />
          <div className="wizard-actions">
            <button type="button" disabled={busy || !isValidOrigin(draft)} onClick={() => void save()}>
              {t('cases.detail.saveOrigin')}
            </button>
            <button type="button" className="secondary" disabled={busy} onClick={() => {
                setDraft(null);
                setError('');
              }}>
              {t('cases.detail.cancelEdit')}
            </button>
          </div>
        </div>
      )}
      {error && (
        <p className="message" role="alert">
          {error}
        </p>
      )}
    </>
  );
}
