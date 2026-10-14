/** What the map's filter accepts on this deployment (`/api/filter-caps`), and the client's side of an
 *  indexed-only deployment (`functions/_lib/indexedOnly.ts`): its inline refusals, its one-form help, and
 *  the server's structured 400s turned into the filter box's message. */
import { useQuery } from '@tanstack/react-query'
import type { QuerySyntax } from '../functions/_lib/queryAst'
import { INDEXED_HELP } from '../functions/_lib/indexedOnly'
import { simple } from '../functions/_lib/querySyntax'
import { useStore, useStoreFetch } from './store'

/** `rootLabel`: the store root's label (`ROOT_LABEL`), the unfiltered view's root crumb; `rootTitle`: that
 *  crumb's tooltip (`ROOT_TITLE`) — each absent when the deployment sets none, or before it loads. */
export interface FilterCaps { indexedOnly: boolean; rootLabel?: string; rootTitle?: string }

/** The deployment's filter capabilities for this subtree's store (unloaded or unreachable: none, i.e. every
 *  form, and no root label). */
export function useFilterCaps(): FilterCaps {
  const store = useStore()
  const sfetch = useStoreFetch()
  const q = useQuery({
    queryKey: ['filter-caps', store.key],
    queryFn: async (): Promise<FilterCaps> => {
      const r = await sfetch('/api/filter-caps', { credentials: 'include' })
      if (!r.ok) return { indexedOnly: false }
      return await r.json() as FilterCaps
    },
    staleTime: Infinity,
    retry: 1,
  })
  return q.data ?? { indexedOnly: false }
}

/** The filter box's "syntax" on an indexed-only deployment: `simple`'s parse, the one-form help. */
export const INDEXED_SYNTAX: QuerySyntax = { id: simple.id, parse: q => simple.parse(q), describe: () => INDEXED_HELP }

/** The refusal codes an API answers a filter with (`functions/_lib/indexedOnly.ts`): a reason to show
 *  inline, never a failure to retry. */
const REFUSAL = /^(?:unsupported-[a-z-]+|scan-not-indexed|term-too-common|anchor-not-indexed)$/

/** A failed API response: `message` as `apiErrorMessage` words it; `refusal` the structured
 *  `{ error, code }` body's reason when its code is a filter refusal (`REFUSAL`); `status` the HTTP status. */
export class ApiError extends Error {
  constructor(message: string, readonly refusal?: { code: string; reason: string }, readonly status?: number) { super(message); this.name = 'ApiError' }
}

/** A failed API response as a throwable `ApiError`. */
export function apiError(status: number, text: string): ApiError {
  let refusal: ApiError['refusal']
  try {
    const j = JSON.parse(text) as { error?: unknown; code?: unknown }
    if (status === 400 && typeof j.error === 'string' && typeof j.code === 'string' && REFUSAL.test(j.code)) refusal = { code: j.code, reason: j.error }
  } catch { /* not JSON */ }
  return new ApiError(apiErrorMessage(status, text), refusal, status)
}

/** An error's filter refusal (a reason to state inline, with no retry or status), else null. */
export const refusalOf = (e: unknown): { code: string; reason: string } | null => (e instanceof ApiError && e.refusal) || null

/** How a failed load reads inline: a filter refusal's reason, or the failure's message — with a retry when it may
 *  pass next time (a 5xx, or no response at all), never for a 4xx (the same request fails the same way). */
export type Failure = { refusal: string } | { message: string; retry: boolean }
export function failureOf(e: unknown): Failure {
  const r = refusalOf(e)
  if (r) return { refusal: r.reason }
  const status = e instanceof ApiError ? e.status : undefined
  return { message: e instanceof Error ? e.message : String(e), retry: status == null || status >= 500 }
}

/** A failed API response as an error message: a structured refusal (`{ error, code }`) as the filter box's
 *  `bad query: …` (shown under the box), anything else as its status and text. */
export function apiErrorMessage(status: number, text: string): string {
  try {
    const j = JSON.parse(text) as { error?: unknown; code?: unknown }
    if (status === 400 && typeof j.error === 'string' && typeof j.code === 'string') return `400: bad query: ${j.error}`
  } catch { /* not JSON */ }
  return `${status}: ${text.slice(0, 120)}`
}

/** The scans the static name index covers (`/api/filter-scans`), when `enabled`: `'loading'` until known,
 *  null when there's no static filter (or the list can't be had: no preference), else the ids. */
export function useIndexedScans(enabled: boolean): string[] | null | 'loading' {
  const q = useQuery({
    queryKey: ['filter-scans'],
    queryFn: async (): Promise<string[] | null> => {
      const r = await fetch('/api/filter-scans', { credentials: 'include' })
      if (!r.ok) return null
      return (await r.json() as { scans: string[] | null }).scans
    },
    enabled,
    staleTime: 60_000,
    refetchInterval: 5 * 60_000,
  })
  return !enabled ? null : q.isPending ? 'loading' : q.data ?? null
}
