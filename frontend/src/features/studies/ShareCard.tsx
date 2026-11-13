import { useEffect, useRef, useState } from 'react';
import { Trans, useTranslation } from 'react-i18next';
import { errorText } from '../../api/client';
import type { ShareLink, Study } from '../../api/types';
import { useMessage } from '../../shared/messages';
import { useFormatters } from '../../shared/useFormatters';
import { useAuth } from '../auth/AuthContext';
import { createShareLink, listShareLinks, revokeShareLink } from './api';

export function ShareCard({ study, onClose }: { study: Study; onClose: () => void }) {
  const { t } = useTranslation();
  const { api } = useAuth();
  const { setMessage } = useMessage();
  const { date } = useFormatters();
  const [links, setLinks] = useState<ShareLink[]>([]);
  const [generatedUrl, setGeneratedUrl] = useState('');
  const urlInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    setGeneratedUrl('');
    setLinks([]);
    listShareLinks(api, study.id)
      .then((payload) => {
        if (!cancelled) setLinks(payload);
      })
      .catch((error: unknown) => setMessage(errorText(error, t('studies.share.loadFailed'))));
    return () => {
      cancelled = true;
    };
  }, [api, study.id, setMessage, t]);

  async function handleCreate() {
    try {
      const payload = await createShareLink(api, study.id);
      setGeneratedUrl(payload.url ?? '');
      setLinks((current) => [payload, ...current]);
      setMessage(t('studies.share.createdMessage'));
    } catch (error) {
      setMessage(errorText(error, t('studies.share.createFailed')));
    }
  }

  async function handleRevoke(link: ShareLink) {
    if (!window.confirm(t('studies.share.confirmRevoke'))) return;
    try {
      const payload = await revokeShareLink(api, study.id, link.id);
      setLinks((current) => current.map((item) => (item.id === payload.id ? payload : item)));
      setMessage(t('studies.share.revokedMessage'));
    } catch (error) {
      setMessage(errorText(error, t('studies.share.revokeFailed')));
    }
  }

  async function handleCopy() {
    if (!generatedUrl) return;
    try {
      await navigator.clipboard.writeText(generatedUrl);
      setMessage(t('studies.share.copied'));
    } catch {
      urlInput.current?.focus();
      urlInput.current?.select();
      if (document.execCommand?.('copy')) {
        setMessage(t('studies.share.copied'));
        return;
      }
      setMessage(t('studies.share.copyFailed'));
    }
  }

  function linkStatus(link: ShareLink): string {
    if (link.is_revoked) return t('studies.share.revoked');
    if (link.is_expired) return t('studies.share.expired');
    return t('studies.share.active');
  }

  return (
    <section className="card detail-card">
      <div className="section-title">
        <div>
          <h2>{t('studies.share.title')}</h2>
          <p className="hint">
            <Trans
              i18nKey="studies.share.hint"
              values={{ filename: study.original_filename }}
              components={{ strong: <strong /> }}
            />
          </p>
        </div>
        <button className="secondary" onClick={onClose}>
          {t('common.close')}
        </button>
      </div>
      <div className="share-actions">
        <button onClick={handleCreate}>{t('studies.share.create')}</button>
        {generatedUrl && (
          <div className="share-url">
            <input
              ref={urlInput}
              type="url"
              value={generatedUrl}
              readOnly
              onFocus={(event) => event.target.select()}
            />
            <button className="secondary" onClick={handleCopy}>
              {t('studies.share.copy')}
            </button>
          </div>
        )}
      </div>
      <div className="table-wrap small-table">
        <table>
          <thead>
            <tr>
              <th>{t('studies.share.created')}</th>
              <th>{t('studies.share.expires')}</th>
              <th>{t('studies.share.accesses')}</th>
              <th>{t('studies.share.status')}</th>
              <th>{t('studies.share.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {links.map((link) => (
              <tr key={link.id}>
                <td>{date(link.created_at)}</td>
                <td>{date(link.expires_at)}</td>
                <td>{link.access_count}</td>
                <td>{linkStatus(link)}</td>
                <td>
                  {!link.is_revoked && !link.is_expired ? (
                    <button className="danger compact" onClick={() => handleRevoke(link)}>
                      {t('studies.share.revoke')}
                    </button>
                  ) : (
                    <span className="muted">{t('common.noActions')}</span>
                  )}
                </td>
              </tr>
            ))}
            {links.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  {t('studies.share.empty')}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
