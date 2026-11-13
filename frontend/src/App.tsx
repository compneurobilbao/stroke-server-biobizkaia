import { Navigate, Route, Routes } from 'react-router-dom';
import { AdminPage } from './features/admin/AdminPage';
import { AuthProvider } from './features/auth/AuthContext';
import { RequireAdmin, RequireAuth } from './features/auth/guards';
import { LoginPage } from './features/auth/LoginPage';
import { NotificationsPage } from './features/notifications/NotificationsPage';
import { StudiesPage } from './features/studies/StudiesPage';
import { Layout } from './layout/Layout';
import { MessageProvider } from './shared/messages';

/** Application shell. The caller supplies the router (BrowserRouter in the app, MemoryRouter in tests). */
export function App() {
  return (
    <MessageProvider>
      <AuthProvider>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/login" element={<LoginPage />} />
            <Route element={<RequireAuth />}>
              <Route path="/studies" element={<StudiesPage />} />
              <Route path="/notifications" element={<NotificationsPage />} />
              <Route element={<RequireAdmin />}>
                <Route path="/admin" element={<AdminPage />} />
              </Route>
            </Route>
            <Route path="*" element={<Navigate to="/studies" replace />} />
          </Route>
        </Routes>
      </AuthProvider>
    </MessageProvider>
  );
}
