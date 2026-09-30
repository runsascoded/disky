import type { Page } from '@playwright/test'

// Console/page noise we don't want to fail the "no console errors" assertion on
// (favicon/manifest 404s, ResizeObserver loop warnings the browser emits).
const IGNORE = [/favicon/i, /manifest/i, /ResizeObserver/i, /\.ico\b/i]

/** Collect genuine console + page errors; returns a live array to assert on. */
export function collectErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('console', m => {
    if (m.type() === 'error' && !IGNORE.some(re => re.test(m.text()))) errors.push(m.text())
  })
  page.on('pageerror', e => errors.push(String(e)))
  return errors
}

// A size cell like "229 Ti" / "126 Gi" — proof the table/estate actually loaded.
export const SIZE_RE = /\d+(\.\d+)?\s*(Ti|Gi|Ki|Mi|B)\b/
