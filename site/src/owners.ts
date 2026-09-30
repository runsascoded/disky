// Ownership-ledger plumbing (specs/actions-ledger.md, owner axis): TSQ
// bindings for /api/actions and the most-recent-wins resolver the map and the
// children table use. The ledger is an append-only WAL of assignments; the
// API serves the live expanded owner rows, and this module folds them: for a
// path, the effective owner is the most recent live row on an
// ancestor-or-equal prefix (recency beats specificity — a newer broad
// assignment repaints older deeper ones; a newer release clears them).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'

export interface OwnerRow {
  prefix: string
  owner: string | null
  ts: number
  who: string
  memo: string | null
  action_id: number
}

/** The resolved (effective) owner of a prefix, with provenance. */
export interface Owner {
  prefix: string
  /** Canonical user id (or email, for pre-mapping claims). */
  who: string
  ts: number
  /** The assigner (`actions.actor`) and their memo — provenance. */
  by: string
  memo: string | null
}

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

export interface OwnerIndex {
  claimOf: (uri: string) => Owner | null
  /** Latest live row per prefix (normalized, trailing `/`). */
  owners: Map<string, OwnerRow>
  count: number
}

export const newer = (a: { ts: number; action_id: number }, b: { ts: number; action_id: number }): boolean =>
  a.ts > b.ts || (a.ts === b.ts && a.action_id > b.action_id)

/** Latest live row per prefix (the API may return history rows per prefix). */
function foldLatest<R extends { prefix: string; ts: number; action_id: number }>(rows: R[]): Map<string, R> {
  const m = new Map<string, R>()
  for (const r of rows) {
    const cur = m.get(r.prefix)
    if (!cur || newer(r, cur)) m.set(r.prefix, r)
  }
  return m
}

/**
 * Lookups are O(depth): a prefix's owner is decided by the newest live row on
 * one of its ancestors-or-self, so `claimOf` walks the ~6 ancestor prefixes
 * and probes a Map — not a scan over every assignment.
 */
export function useOwnerIndex(data: { owners: OwnerRow[] } | undefined): OwnerIndex {
  return useMemo(() => {
    const norm = (uri: string) => (uri.endsWith('/') ? uri : uri + '/')
    const owners = foldLatest((data?.owners ?? []).map(r => (r.prefix.endsWith('/') ? r : { ...r, prefix: r.prefix + '/' })))
    // 'gs://b/x/y/' → ['gs://b/', 'gs://b/x/', 'gs://b/x/y/'] (self last).
    const ancestors = (p: string): string[] => {
      const out: string[] = []
      let i = p.indexOf('/', 'gs://'.length)
      while (i !== -1) {
        out.push(p.slice(0, i + 1))
        i = p.indexOf('/', i + 1)
      }
      return out
    }
    const claimOf = (uri: string): Owner | null => {
      const p = norm(uri)
      let win: OwnerRow | null = null
      for (const a of ancestors(p)) {
        const r = owners.get(a)
        if (r && (!win || newer(r, win))) win = r
      }
      return win?.owner != null ? { prefix: win.prefix, who: win.owner, ts: win.ts, by: win.who, memo: win.memo } : null
    }
    let count = 0
    for (const r of owners.values()) if (r.owner != null) count++
    return { claimOf, owners, count }
  }, [data])
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
