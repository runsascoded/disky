/** Shared wire contract. Disky-specific activity classification is retained pending upstream adoption. */
export * from '@open-athena/auth/session-log'
import type { SlogEvent } from '@open-athena/auth/session-log'
import { summarize as sharedSummary } from '@open-athena/auth/react/session-log'

export const benignError = (msg: unknown): boolean => typeof msg === 'string' && /^ResizeObserver loop /.test(msg)
export const isCancelled = (e: { k?: unknown; s?: unknown; err?: unknown }): boolean => e.k === 'api' && e.s === 0 && e.err === 'abort'
export const isFailedApi = (e: { k?: unknown; s?: unknown; err?: unknown }): boolean =>
  e.k === 'api' && typeof e.s === 'number' && (e.s >= 500 || (e.s === 0 && !isCancelled(e)))
export const isErrorEvent = (e: { k: string; s?: unknown; err?: unknown }): boolean =>
  e.k === 'error' || e.k === 'reject' || e.k === 'console' || isFailedApi(e)
export function summarize(e: SlogEvent): string {
  if (isCancelled(e)) return `${e.m || 'GET'} ${e.u || ''} → cancelled · ${e.ms ?? ''}ms`
  return sharedSummary({ ...e, pid: '', seq: 0 })
}
