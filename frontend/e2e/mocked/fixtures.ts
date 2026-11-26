import { expect, test as base } from '@playwright/test';

export { expect };
export type { Page } from '@playwright/test';

/**
 * `test` for the mocked specs. `vite preview` serves the security headers of the reverse proxy
 * (`playwright.config.ts`), so every spec runs under its Content-Security-Policy; this automatic
 * fixture fails the test on any CSP violation reported by the page.
 */
export const test = base.extend<{ cspViolations: string[] }>({
  cspViolations: [
    async ({ page }, use) => {
      const violations: string[] = [];
      await page.exposeFunction('__reportCspViolation', (text: string) => {
        violations.push(text);
      });
      await page.addInitScript(() => {
        document.addEventListener('securitypolicyviolation', (event) => {
          const report = (window as unknown as { __reportCspViolation: (text: string) => void }).__reportCspViolation;
          report(`${event.effectiveDirective} blocked ${event.blockedURI || 'inline'} (${event.sourceFile}:${event.lineNumber})`);
        });
      });
      page.on('console', (message) => {
        if (message.text().includes('Content Security Policy')) violations.push(message.text());
      });
      await use(violations);
      expect(violations, 'Content-Security-Policy violations').toEqual([]);
    },
    { auto: true },
  ],
});
