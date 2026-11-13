import { useCallback, useEffect, useState } from 'react';
import type { AdminDashboard } from '../../api/types';
import { useAuth } from '../auth/AuthContext';
import { getDashboard } from './api';
import { DashboardCard } from './DashboardCard';
import { UsersCard } from './UsersCard';

const POLL_INTERVAL_MS = 10000;

export function AdminPage() {
  const { api } = useAuth();
  const [dashboard, setDashboard] = useState<AdminDashboard | null>(null);

  const loadDashboard = useCallback(async () => {
    try {
      setDashboard(await getDashboard(api));
    } catch {
      // Silent, as before.
    }
  }, [api]);

  useEffect(() => {
    void loadDashboard();
    const timer = window.setInterval(() => void loadDashboard(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [loadDashboard]);

  return (
    <>
      {dashboard && <DashboardCard dashboard={dashboard} onRefresh={() => void loadDashboard()} />}
      <UsersCard onChanged={() => void loadDashboard()} />
    </>
  );
}
