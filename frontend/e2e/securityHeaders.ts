import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/** The reverse proxy configuration: the single source of the security headers (CSP included). */
export const PROXY_CONF = fileURLToPath(new URL('../../infra/reverse-proxy/nginx.conf', import.meta.url));

/** Headers the reverse proxy sets with `add_header <Name> "<value>" always;`. */
export function proxySecurityHeaders(path: string = PROXY_CONF): Record<string, string> {
  const headers: Record<string, string> = {};
  const pattern = /^\s*add_header\s+([A-Za-z-]+)\s+"([^"]*)"\s+always;/gm;
  for (const match of readFileSync(path, 'utf8').matchAll(pattern)) {
    headers[match[1]] = match[2];
  }
  return headers;
}
