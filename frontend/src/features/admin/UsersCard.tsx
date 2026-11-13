import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { errorText } from '../../api/client';
import type { Role, User } from '../../api/types';
import { formatBytes, quotaBytesFromMbInput } from '../../shared/format';
import { useMessage } from '../../shared/messages';
import { useFormatters } from '../../shared/useFormatters';
import { useAuth } from '../auth/AuthContext';
import { createUser, deleteUser, listUsers, updateUserQuota } from './api';

interface NewUserForm {
  email: string;
  full_name: string;
  password: string;
  role: Role;
  storage_quota_mb: string;
}

const EMPTY_FORM: NewUserForm = { email: '', full_name: '', password: '', role: 'researcher', storage_quota_mb: '' };

export function UsersCard({ onChanged }: { onChanged: () => void }) {
  const { t } = useTranslation();
  const { api, user } = useAuth();
  const { setMessage } = useMessage();
  const { quota } = useFormatters();
  const [users, setUsers] = useState<User[]>([]);
  const [form, setForm] = useState<NewUserForm>(EMPTY_FORM);
  const [loading, setLoading] = useState(false);

  const loadUsers = useCallback(async () => {
    try {
      setUsers(await listUsers(api));
    } catch {
      // Silent, as before.
    }
  }, [api]);

  useEffect(() => {
    void loadUsers();
  }, [loadUsers]);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (form.password.length < 8) {
      setMessage(t('admin.users.passwordTooShort'));
      return;
    }
    setLoading(true);
    setMessage(t('admin.users.creating'));
    try {
      const created = await createUser(api, {
        email: form.email,
        full_name: form.full_name,
        password: form.password,
        role: form.role,
        storage_quota_bytes: quotaBytesFromMbInput(form.storage_quota_mb),
      });
      setForm(EMPTY_FORM);
      setMessage(t('admin.users.created', { email: created.email }));
      await loadUsers();
      onChanged();
    } catch (error) {
      setMessage(errorText(error, t('admin.users.createFailed')));
    } finally {
      setLoading(false);
    }
  }

  async function handleQuota(item: User) {
    const currentValue =
      item.storage_quota_bytes == null ? '' : String(Math.round(item.storage_quota_bytes / 1024 / 1024));
    const value = window.prompt(t('admin.users.quotaPrompt'), currentValue);
    if (value === null) return;
    try {
      const payload = await updateUserQuota(api, item.id, quotaBytesFromMbInput(value));
      setUsers((current) => current.map((userItem) => (userItem.id === payload.id ? payload : userItem)));
      setMessage(t('admin.users.quotaUpdated'));
    } catch (error) {
      setMessage(errorText(error, t('admin.users.quotaFailed')));
    }
  }

  async function handleDelete(item: User) {
    if (!window.confirm(t('admin.users.confirmDelete', { email: item.email }))) return;
    try {
      const payload = await deleteUser(api, item.id);
      setMessage(payload.message || t('admin.users.deleted'));
      await loadUsers();
      onChanged();
    } catch (error) {
      setMessage(errorText(error, t('admin.users.deleteFailed')));
    }
  }

  return (
    <section className="card">
      <div className="section-title">
        <h2>{t('admin.users.title')}</h2>
        <button className="secondary" onClick={() => void loadUsers()}>
          {t('admin.users.refresh')}
        </button>
      </div>
      <form onSubmit={handleCreate}>
        <input
          type="email"
          value={form.email}
          onChange={(event) => setForm({ ...form, email: event.target.value })}
          placeholder={t('admin.users.emailPlaceholder')}
          required
        />
        <input
          type="text"
          value={form.full_name}
          onChange={(event) => setForm({ ...form, full_name: event.target.value })}
          placeholder={t('admin.users.namePlaceholder')}
          required
        />
        <input
          type="password"
          minLength={8}
          value={form.password}
          onChange={(event) => setForm({ ...form, password: event.target.value })}
          placeholder={t('admin.users.passwordPlaceholder')}
          required
        />
        <select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value as Role })}>
          <option value="researcher">researcher</option>
          <option value="admin">admin</option>
        </select>
        <input
          type="number"
          min="0"
          value={form.storage_quota_mb}
          onChange={(event) => setForm({ ...form, storage_quota_mb: event.target.value })}
          placeholder={t('admin.users.quotaPlaceholder')}
          aria-label={t('admin.users.quotaLabel')}
        />
        <button disabled={loading}>{t('admin.users.create')}</button>
      </form>
      <p className="hint">{t('admin.users.passwordHint')}</p>
      <div className="table-wrap small-table">
        <table>
          <thead>
            <tr>
              <th>{t('admin.users.email')}</th>
              <th>{t('admin.users.name')}</th>
              <th>{t('admin.users.role')}</th>
              <th>{t('admin.users.active')}</th>
              <th>{t('admin.users.storage')}</th>
              <th>{t('admin.users.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {users.map((item) => (
              <tr key={item.id}>
                <td>{item.email}</td>
                <td>{item.full_name}</td>
                <td>{item.role}</td>
                <td>{item.is_active ? t('common.yes') : t('common.no')}</td>
                <td>
                  {formatBytes(item.storage_used_bytes)} / {quota(item.storage_quota_bytes)}
                </td>
                <td className="actions-cell">
                  <button className="secondary compact" onClick={() => void handleQuota(item)}>
                    {t('admin.users.quota')}
                  </button>
                  {item.id !== user?.id ? (
                    <button className="danger compact" onClick={() => void handleDelete(item)}>
                      {t('admin.users.delete')}
                    </button>
                  ) : (
                    <span className="muted">{t('admin.users.currentUser')}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
