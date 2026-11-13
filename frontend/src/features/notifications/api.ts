import type { ApiClient } from '../../api/client';
import type { NotificationItem, NotificationPrefs } from '../../api/types';

export const listNotifications = (api: ApiClient) => api.json<NotificationItem[]>('/notifications');

export const markNotificationRead = (api: ApiClient, id: string) =>
  api.json<NotificationItem>(`/notifications/${id}/read`, { method: 'POST' });

export const getNotificationPreferences = (api: ApiClient) =>
  api.json<NotificationPrefs>('/me/notification-preferences');

export const updateNotificationPreferences = (api: ApiClient, prefs: NotificationPrefs) =>
  api.json<NotificationPrefs>('/me/notification-preferences', { method: 'PATCH', json: prefs });
