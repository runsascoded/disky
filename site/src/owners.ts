// Ownership-ledger plumbing (specs/actions-ledger.md, owner axis): TSQ
// bindings for /api/actions and the most-recent-wins resolver the map and the
// children table use. The ledger is an append-only WAL of assignments; the
// API serves the live expanded owner rows, and this module folds them: for a
// path, the effective owner is the most recent live row on an
// ancestor-or-equal prefix (recency beats specificity — a newer broad
// assignment repaints older deeper ones; a newer release clears them).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'

export { foldLatest, newer, ownerIndex } from './ownerIndex'
export type { Owner, OwnerIndex, OwnerRow } from './ownerIndex'
import { ownerIndex, type OwnerIndex, type OwnerRow } from './ownerIndex'

// 30s poll: several people assign concurrently, and the map should reflect
// their assignments without a reload.
export function useOwners(enabled: boolean) {
  return useQuery<{ owners: OwnerRow[] }, Error>({
    queryKey: ['actions'],
    enabled,
    refetchInterval: 30_000,
    queryFn: async () => {
      const r = await fetch('/api/actions', { credentials: 'include' })
      if (!r.ok) throw new Error(`actions: ${r.status}`)
      return r.json()
    },
  })
}

/** One POSTable assignment; `owner: null` clears, `'@me'` = the server
 * resolves the actor's canonical user id. */
export interface OwnerPost {
  pattern: string
  owner: string | null
  memo?: string
  scan?: string
}

// The scan id the viewer is looking at, stamped onto posted actions. Set by
// App (module-level: mutations fire from deep components that don't
// otherwise care which scan is showing).
let currentScan: string | undefined
export const setCurrentScan = (s?: string) => { currentScan = s }

export function useOwnerMutations() {
  const qc = useQueryClient()
  const post = useMutation({
    mutationFn: async (v: OwnerPost | OwnerPost[]) => {
      const items = (Array.isArray(v) ? v : [v]).map(a => ({ scan: currentScan, ...a }))
      const r = await fetch('/api/actions', {
        method: 'POST',
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(items.length === 1 ? items[0] : items),
      })
      if (!r.ok) throw new Error(((await r.json()) as { error?: string }).error ?? `${r.status}`)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['actions'] })
      void qc.invalidateQueries({ queryKey: ['estate'] })
    },
  })
  // `owner` omitted → assign to the actor (`'@me'`); a canonical user id →
  // assign it to that user; `release: true` → clear ownership.
  const assign = {
    mutate: (v: { prefix: string; owner?: string | null; release?: boolean }) =>
      post.mutate({ pattern: v.prefix, owner: v.release ? null : (v.owner ?? '@me') }),
    error: post.error,
  }
  return { post, assign }
}

export function useOwnerIndex(data: { owners: OwnerRow[] } | undefined): OwnerIndex {
  return useMemo(() => ownerIndex(data), [data])
}

/** D1 `user_emails` as an email → canonical-user map (signed-in readers only). */
export function useUserEmails(enabled: boolean): Record<string, string> | undefined {
  const { data } = useQuery<Record<string, string>, Error>({
    queryKey: ['user-emails'],
    enabled,
    staleTime: 10 * 60_000,
    retry: false,
    queryFn: async () => {
      const r = await fetch('/api/db/user_emails', { credentials: 'include' })
      if (!r.ok) throw new Error(`user_emails: ${r.status}`)
      const { rows } = (await r.json()) as { rows: { email: string; user: string }[] }
      return Object.fromEntries(rows.map(x => [x.email, x.user]))
    },
  })
  return data
}

/** The viewer's canonical attribution user id, from D1 `user_emails`. */
export function useMyUser(email: string | undefined, enabled: boolean): string | null {
  const data = useUserEmails(enabled && !!email)
  return (email && data?.[email.toLowerCase()]) || null
}
