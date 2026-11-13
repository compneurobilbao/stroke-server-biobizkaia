import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { errorText } from '../../api/client';
import type { NotificationItem, NotificationPrefs } from '../../api/types';
import { useMessage } from '../../shared/messages';
import { useFormatters } from '../../shared/useFormatters';
import { useAuth } from '../auth/AuthContext';
import {
  getNotificationPreferences,
  listNotifications,
  markNotificationRead,
  updateNotificationPreferences,
} from './api';

const POLL_INTERVAL_MS = 15000;
const DEFAULT_PREFS: NotificationPrefs = { notify_on_processing_completed: true, notify_on_processing_failed: true };

function NotificationList({
  notifications,
  onRead,
}: {
  notifications: NotificationItem[];
  onRead: (notification: NotificationItem) => void;
}) {
  const { t } = useTranslation();
  const { date } = useFormatters();
  const unread = notifications.filter((notification) => !notification.read_at).length;
  return (
    <div className="notification-list">
      <p className="hint">{unread ? t('notifications.unread', { count: unread }) : t('notifications.noneUnread')}</p>
      {notifications.map((notification) => (
        <article
          key={notification.id}
          className={`notification-item ${notification.read_at ? 'read' : 'unread'}`}
        >
          <div>
            <strong>{notification.title}</strong>
            <p>{notification.message}</p>
            <span className="muted">
              {date(notification.created_at)} ·{' '}
              {t('notifications.email', {
                status: t(`notifications.emailStatus.${notification.email_status}`, {
                  defaultValue: notification.email_status,
                }),
              })}
            </span>
          </div>
          {!notification.read_at && (
            <button className="secondary compact" onClick={() => onRead(notification)}>
              {t('notifications.markRead')}
            </button>
          )}
        </article>
      ))}
      {notifications.length === 0 && <p className="muted">{t('notifications.empty')}</p>}
    </div>
  );
}

export function NotificationsPage() {
  const { t } = useTranslation();
  const { api } = useAuth();
  const { setMessage } = useMessage();
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [prefs, setPrefs] = useState<NotificationPrefs>(DEFAULT_PREFS);

  const loadNotifications = useCallback(async () => {
    try {
      setNotifications(await listNotifications(api));
    } catch {
      // Polling failures are silent, as before.
    }
  }, [api]);

  const loadPreferences = useCallback(async () => {
    try {
      setPrefs(await getNotificationPreferences(api));
    } catch {
      // Keep defaults.
    }
  }, [api]);

  useEffect(() => {
    void loadNotifications();
    void loadPreferences();
    const timer = window.setInterval(() => void loadNotifications(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [loadNotifications, loadPreferences]);

  async function handleRead(notification: NotificationItem) {
    try {
      const payload = await markNotificationRead(api, notification.id);
      setNotifications((current) => current.map((item) => (item.id === payload.id ? payload : item)));
    } catch (error) {
      setMessage(errorText(error, t('notifications.markFailed')));
    }
  }

  async function handlePreference(name: keyof NotificationPrefs, checked: boolean) {
    const next = { ...prefs, [name]: checked };
    setPrefs(next);
    try {
      setPrefs(await updateNotificationPreferences(api, next));
      setMessage(t('notifications.prefsSaved'));
    } catch (error) {
      setMessage(errorText(error, t('notifications.prefsFailed')));
      await loadPreferences();
    }
  }

  return (
    <section className="card notifications-card">
      <div className="section-title">
        <div>
          <h2>{t('notifications.title')}</h2>
          <p className="hint">{t('notifications.hint')}</p>
        </div>
        <button className="secondary" onClick={() => void loadNotifications()}>
          {t('common.refresh')}
        </button>
      </div>
      <div className="notification-preferences">
        <label>
          <input
            type="checkbox"
            checked={prefs.notify_on_processing_completed}
            onChange={(event) => void handlePreference('notify_on_processing_completed', event.target.checked)}
          />
          {t('notifications.emailOnCompleted')}
        </label>
        <label>
          <input
            type="checkbox"
            checked={prefs.notify_on_processing_failed}
            onChange={(event) => void handlePreference('notify_on_processing_failed', event.target.checked)}
          />
          {t('notifications.emailOnFailed')}
        </label>
      </div>
      <NotificationList notifications={notifications} onRead={(item) => void handleRead(item)} />
    </section>
  );
}
