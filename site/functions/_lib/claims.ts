/**
 * Per-user owned bytes from the ownership ledger + the floor-free path index
 * (specs/done/path-agnostic-serving.md §2.3; the band model of
 * specs/exact-state-totals.md, owner axis only). Pure: callers supply the
 * folded claims and the index aggregates for the prefixes involved; nothing
 * here does I/O.
 *
 * Model. Every live claim is a node of a trie `U`. Its *band* is its own
 * bytes minus the bytes of its nearest claimed descendants — the exclusive
 * slice no deeper claim repaints. The band belongs wholly to its *effective*
 * claimant: the newest claim on an ancestor-or-self (recency beats
 * specificity — a newer broad claim repaints older deeper ones, a newer
 * release repaints them back to the scan's attribution). A band under no
 * claimant is split by the scan's per-user slices. Bands partition the
 * estate, so a user's total is exact.
 */

export interface LedgerRow { prefix: string; ts: number; action_id: number }
export interface OwnerRow extends LedgerRow { owner: string | null; who?: string }

/** Per-path aggregate from the index: bytes, objects, per-user bytes, and
 * non-STANDARD class bytes ("2" NL, "3" CL, "4" AR; STANDARD = b − Σ). */
export interface PathAgg { b: number; o: number; us: Record<string, number>; cb: Record<string, number> }
export const newAgg = (): PathAgg => ({ b: 0, o: 0, us: {}, cb: {} })
export const addAgg = (a: PathAgg, r: { size: number; n_files: number; usr: string | null; cls2: number; cls3: number; cls4: number }): void => {
  a.b += r.size
  a.o += r.n_files
  if (r.usr) a.us[r.usr] = (a.us[r.usr] ?? 0) + r.size
  for (const [k, v] of [['2', r.cls2], ['3', r.cls3], ['4', r.cls4]] as [string, number][]) if (v) a.cb[k] = (a.cb[k] ?? 0) + v
}
const subAgg = (a: PathAgg, kids: PathAgg[]): PathAgg => {
  const out: PathAgg = { b: a.b, o: a.o, us: { ...a.us }, cb: { ...a.cb } }
  for (const k of kids) {
    out.b -= k.b
    out.o -= k.o
    for (const [u, b] of Object.entries(k.us)) out.us[u] = (out.us[u] ?? 0) - b
    for (const [c, b] of Object.entries(k.cb)) out.cb[c] = (out.cb[c] ?? 0) - b
  }
  out.b = Math.max(0, out.b)
  out.o = Math.max(0, out.o)
  for (const m of [out.us, out.cb]) for (const k of Object.keys(m)) if (m[k] <= 0) delete m[k]
  return out
}
/** Full class mix (STANDARD implied) scaled to `b` of the aggregate's bytes. */
const mixOf = (a: PathAgg, b: number): Record<string, number> => {
  if (a.b <= 0 || b <= 0) return {}
  const cold = Object.values(a.cb).reduce((s, v) => s + v, 0)
  const f = b / a.b
  const out: Record<string, number> = {}
  if (a.b > cold) out['1'] = (a.b - cold) * f
  for (const [c, v] of Object.entries(a.cb)) out[c] = v * f
  return out
}

export const newer = (a: LedgerRow, b: LedgerRow): boolean => a.ts > b.ts || (a.ts === b.ts && a.action_id > b.action_id)
export const norm = (p: string): string => (p.endsWith('/') ? p : p + '/')

/** Latest live row per (normalized) prefix — the API returns history rows. */
export function foldLatest<R extends LedgerRow>(rows: R[]): Map<string, R> {
  const m = new Map<string, R>()
  for (const raw of rows) {
    const r = raw.prefix.endsWith('/') ? raw : { ...raw, prefix: raw.prefix + '/' }
    const cur = m.get(r.prefix)
    if (!cur || newer(r, cur)) m.set(r.prefix, r)
  }
  return m
}

/** `gs://marin-b/x/y/` → index key `marin-b/x/y` at depth 3. */
export const idxKey = (prefix: string): { path: string; depth: number } => {
  const path = prefix.replace(/^[a-z0-9]+:\/\//, '').replace(/\/+$/, '')
  return { path, depth: path.split('/').length }
}

/** One person's owned bytes (claims applied) and the storage-class mix behind
 * them (class id → bytes; STANDARD = "1"), so the estate can be priced like
 * the scan's attribution is. A user's share of a band is assumed to carry the
 * band's class mix (the index has no per-user class split). */
export interface UserOwned { b: number; mix: Record<string, number> }

/** A live owner claim, sized from the index (bytes under its prefix). */
export interface ClaimRow {
  prefix: string
  owner: string | null
  /** The assigner (`actions.actor`) — always a person today. */
  who?: string
  ts: number
  action_id: number
  bytes: number
  objects: number
  /** The subtree's bytes per scan-attributed user (what the claim
   * repaints) — the owner lens subtracts and adds these per band. */
  us: Record<string, number>
  /** Set when a newer ancestor claim overrides this one. */
  repainted_by?: string
}

export interface OwnerTotals {
  bytes: number
  objects: number
  users: Record<string, UserOwned>
  claims: ClaimRow[]
}

export interface OwnersInput {
  owners: Map<string, OwnerRow>
  /** Aggregates by index path for every claim prefix (missing = 0 bytes: the
   * prefix no longer exists in this scan) and for every bucket (depth 1). */
  aggs: Map<string, PathAgg>
  /** Bucket index paths (depth-1 rows present in the scan). */
  buckets: string[]
}

interface Node {
  prefix: string
  path: string
  depth: number
  parent: Node | null
  kids: Node[]
  owner: OwnerRow
  effOwner: OwnerRow | null
  agg: PathAgg
  band: PathAgg
}

export function computeOwners(input: OwnersInput): OwnerTotals {
  const { owners, aggs, buckets } = input
  const nodes = new Map<string, Node>()
  for (const [prefix, r] of owners) {
    const { path, depth } = idxKey(prefix)
    nodes.set(prefix, { prefix, path, depth, parent: null, kids: [], owner: r, effOwner: null, agg: aggs.get(path) ?? newAgg(), band: newAgg() })
  }
  // Parent = nearest strict ancestor in the trie.
  const ancestors = (p: string): string[] => {
    const out: string[] = []
    let i = p.indexOf('/', 'gs://'.length)
    while (i !== -1) { out.push(p.slice(0, i + 1)); i = p.indexOf('/', i + 1) }
    return out.slice(0, -1) // strict
  }
  const ordered = [...nodes.values()].sort((a, b) => a.depth - b.depth || (a.prefix < b.prefix ? -1 : 1))
  for (const n of ordered) {
    for (const a of ancestors(n.prefix).reverse()) {
      const par = nodes.get(a)
      if (par) { n.parent = par; par.kids.push(n); break }
    }
    const inh = n.parent?.effOwner ?? null
    n.effOwner = !inh || newer(n.owner, inh) ? n.owner : inh
  }
  for (const n of nodes.values()) n.band = subAgg(n.agg, n.kids.map(k => k.agg))

  const users: Record<string, UserOwned> = {}
  const userRec = (u: string): UserOwned => (users[u] ??= { b: 0, mix: {} })
  const addMix = (into: Record<string, number>, mix: Record<string, number>, f: number) => {
    for (const [c, b] of Object.entries(mix)) if (b * f > 0) into[c] = (into[c] ?? 0) + b * f
  }
  // A band's bytes go to its claimant (whole band) or to its scan-attributed
  // users (their slices).
  const paint = (band: PathAgg, claimant: string | null) => {
    if (band.b <= 0) return
    const mix = mixOf(band, band.b)
    if (claimant) {
      const u = userRec(claimant)
      u.b += band.b
      addMix(u.mix, mix, 1)
    } else {
      for (const [usr, b] of Object.entries(band.us)) {
        const u = userRec(usr)
        u.b += b
        addMix(u.mix, mix, b / band.b)
      }
    }
  }
  for (const n of nodes.values()) paint(n.band, n.effOwner?.owner ?? null)
  // The remainder of each bucket outside every top-level band is the scan's
  // attribution as-is. A claim exactly on the bucket is the sole top-level
  // node and its band already covers the whole remainder.
  let bytes = 0
  let objects = 0
  for (const bp of buckets) {
    const agg = aggs.get(bp) ?? newAgg()
    bytes += agg.b
    objects += agg.o
    if (nodes.has(`gs://${bp}/`)) continue
    const top = [...nodes.values()].filter(n => !n.parent && n.path.startsWith(bp + '/'))
    paint(subAgg(agg, top.map(n => n.agg)), null)
  }
  const claims: ClaimRow[] = []
  for (const n of nodes.values()) {
    claims.push({
      prefix: n.prefix,
      owner: n.owner.owner,
      who: n.owner.who,
      ts: n.owner.ts,
      action_id: n.owner.action_id,
      bytes: n.agg.b,
      objects: n.agg.o,
      us: Object.fromEntries(Object.entries(n.agg.us).filter(([, b]) => b > 0)),
      ...(n.effOwner === n.owner ? {} : { repainted_by: n.effOwner!.prefix }),
    })
  }
  claims.sort((a, b) => b.bytes - a.bytes)
  for (const u of Object.values(users)) u.b = Math.round(u.b)
  return { bytes, objects, users, claims }
}
