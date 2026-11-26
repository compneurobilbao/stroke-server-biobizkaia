import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// `vite preview` response headers. The mocked Playwright job passes the security headers of the
// reverse proxy here (playwright.config.ts), so the built app is tested under the same CSP.
const previewHeaders = process.env.PREVIEW_SECURITY_HEADERS
  ? (JSON.parse(process.env.PREVIEW_SECURITY_HEADERS) as Record<string, string>)
  : undefined;

export default defineConfig({
  plugins: [react()],
  preview: { headers: previewHeaders },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
