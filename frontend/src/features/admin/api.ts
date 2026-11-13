import type { ApiClient } from '../../api/client';
import type { AdminDashboard, MessageResponse, NewUserPayload, User } from '../../api/types';

export const getDashboard = (api: ApiClient) => api.json<AdminDashboard>('/admin/dashboard');

export const listUsers = (api: ApiClient) => api.json<User[]>('/users');

export const createUser = (api: ApiClient, payload: NewUserPayload) =>
  api.json<User>('/users', { method: 'POST', json: payload });

export const updateUserQuota = (api: ApiClient, id: string, storageQuotaBytes: number | null) =>
  api.json<User>(`/users/${id}`, { method: 'PATCH', json: { storage_quota_bytes: storageQuotaBytes } });

export const deleteUser = (api: ApiClient, id: string) => api.json<MessageResponse>(`/users/${id}`, { method: 'DELETE' });
