// The ownership ledger's most-recent-wins resolver, React-free so the
// Functions (OG cards) fold the ledger exactly as the map does. For a path,
// the effective owner is the most recent live row on an ancestor-or-equal
// prefix (recency beats specificity). `owners.ts` re-exports it.

// An exact-object row (`kind: 'object'`, specs/file-assign.md) owns its one
// key: on that key's chain it sits at the leaf — newer than every folder above
// it, it wins; older, a folder repaints it — and it is on no other path's
// chain (not `key.bak`'s, not `key/…`'s).
export type ItemKind = 'prefix' | 'object'

export interface OwnerRow {
  prefix: string
  /** What `prefix` names (absent = a folder: rows from before `kind`). */
  kind?: ItemKind
  owner: string | null
  ts: number
  who: string
  memo: string | null
  action_id: number
}

/** The resolved (effective) owner of a prefix, with provenance. */
export interface Owner {
  prefix: string
  /** Canonical user id (or email, for pre-mapping assignments). */
  who: string
  ts: number
  /** The assigner (`actions.actor`) and their memo — provenance. */
  by: string
  memo: string | null
}

export interface OwnerIndex {
  /** The effective assignment of a folder (`kind` 'prefix', the default) or one exact object. */
  assignmentOf: (uri: string, kind?: ItemKind) => Owner | null
  /** Latest live folder row per prefix (normalized, trailing `/`). */
  owners: Map<string, OwnerRow>
  /** Latest live exact-object row per key. */
  objects: Map<string, OwnerRow>
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
 * one of its ancestors-or-self, so `assignmentOf` walks the ~6 ancestor prefixes
 * and probes a Map — not a scan over every assignment.
 */
export function ownerIndex(data: { owners: OwnerRow[] } | undefined): OwnerIndex {
  const norm = (uri: string) => (uri.endsWith('/') ? uri : uri + '/')
  const rows = data?.owners ?? []
  const owners = foldLatest(rows.filter(r => r.kind !== 'object').map(r => (r.prefix.endsWith('/') ? r : { ...r, prefix: r.prefix + '/' })))
  const objects = foldLatest(rows.filter(r => r.kind === 'object'))
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
  const assignmentOf = (uri: string, kind: ItemKind = 'prefix'): Owner | null => {
    // A folder: its own prefix and every folder above. An object: every folder
    // above it (all of its `/`-cuts are strict ancestors) and its exact row.
    const chain = kind === 'object' ? ancestors(uri) : ancestors(norm(uri))
    let win: OwnerRow | null = kind === 'object' ? objects.get(uri) ?? null : null
    for (const a of chain) {
      const r = owners.get(a)
      if (r && (!win || newer(r, win))) win = r
    }
    return win?.owner != null ? { prefix: win.prefix, who: win.owner, ts: win.ts, by: win.who, memo: win.memo } : null
  }
  let count = 0
  for (const r of owners.values()) if (r.owner != null) count++
  for (const r of objects.values()) if (r.owner != null) count++
  return { assignmentOf, owners, objects, count }
}
