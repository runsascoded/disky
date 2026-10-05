// The ownership ledger's most-recent-wins resolver, React-free so the
// Functions (OG cards) fold the ledger exactly as the map does. For a path,
// the effective owner is the most recent live row on an ancestor-or-equal
// prefix (recency beats specificity). `owners.ts` re-exports it.

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

export interface OwnerIndex {
  claimOf: (uri: string) => Owner | null
  /** Latest live row per prefix (normalized, trailing `/`). */
  owners: Map<string, OwnerRow>
  count: number
}

export const newer = (a: { ts: number; action_id: number }, b: { ts: number; action_id: number }): boolean =>
  a.ts > b.ts || (a.ts === b.ts && a.action_id > b.action_id)

/** Latest live row per prefix (the API may return history rows per prefix). */
export function foldLatest<R extends { prefix: string; ts: number; action_id: number }>(rows: R[]): Map<string, R> {
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
export function ownerIndex(data: { owners: OwnerRow[] } | undefined): OwnerIndex {
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
}
