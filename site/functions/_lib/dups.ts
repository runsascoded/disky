/**
 * The scan's duplicate-candidate report (specs/dups.md §2): `dups.parquet` (one row per (group, copy), sorted by the
 * group's reclaimable bytes, a group's copies contiguous) and `dups.json` (the summary), which the `dups` scan step
 * writes beside the scan's index (`dt-cloud dups`). A read decodes row groups in order and stops at the `n`+1-th
 * matching group, or at the first group under `min` (the sort makes every later one smaller too).
 */
import type { AsyncBuffer } from 'hyparquet'
import { parquetMetadataAsync, parquetReadObjects } from 'hyparquet'
import { compressors } from './zstd.js'

export type Tier = 'tree' | 'moved'
export const TIERS: readonly Tier[] = ['tree', 'moved']
/** The spot check's verdict on a copy against its group's keep copy (null: unchecked, or the keep copy). */
export type Check = 'equal' | 'partial' | 'differs'

export interface DupCopy {
  path: string
  /** The path minus its bucket (a `moved` group's differ). */
  rel: string
  bucket: string
  size: number
  n_files: number
  /** Epoch seconds (newest write; null: unknown). */
  mtime: number | null
  /** Epoch seconds (oldest object). */
  created: number | null
  /** Epoch days (newest read; null: never read, as far as the logs go). */
  last_read: number | null
  /** Cold-class bytes by storage class id (`2` Nearline, `3` Coldline, `4` Archive); the rest is Standard. */
  cb: Record<string, number>
  /** The suggested copy to keep: the latest read, ties → the oldest. */
  keep: boolean
  /** Under another group's copy: its bytes are counted there. */
  nested: boolean
  check: Check | null
  check_n: number | null
  check_eq: number | null
  /** The open plan staging it (an item covering it), when the API annotates. */
  staged?: number | null
}

export interface DupGroup {
  group: string
  tier: Tier
  /** What the copies share: a `tree` group's relative path, a `moved` group's basename. */
  rel: string
  kind: 'dir' | 'file'
  size: number
  n_files: number
  copies: number
  /** size × (copies − 1). */
  reclaimable: number
  n_buckets: number
  suspect: boolean
  items: DupCopy[]
}

export interface DupFilter {
  tier?: Tier
  /** The least reclaimable bytes. */
  min?: number
  /** A group with copies in each (a bucket named twice: ≥2 copies in it). */
  buckets?: string[]
  /** A substring of some copy's path (case-insensitive). */
  q?: string
  /** Include `suspect` groups (≥ the step's copy threshold; mostly same-size collisions). */
  suspect?: boolean
  /** Include groups whose every copy is nested under another group's. */
  nested?: boolean
}

export function groupMatches(g: DupGroup, f: DupFilter): boolean {
  if (f.tier && g.tier !== f.tier) return false
  if (f.min != null && g.reclaimable < f.min) return false
  if (!f.suspect && g.suspect) return false
  if (!f.nested && g.items.every(c => c.nested)) return false
  if (f.buckets?.length) {
    const have = new Map<string, number>()
    for (const c of g.items) have.set(c.bucket, (have.get(c.bucket) ?? 0) + 1)
    const want = new Map<string, number>()
    for (const b of f.buckets) want.set(b, (want.get(b) ?? 0) + 1)
    for (const [b, n] of want) if ((have.get(b) ?? 0) < n) return false
  }
  if (f.q) {
    const q = f.q.toLowerCase()
    if (!g.items.some(c => c.path.toLowerCase().includes(q))) return false
  }
  return true
}

const CLASS_PREFIX = 'sum_storage_class_id_'
const n = (v: unknown): number => (typeof v === 'bigint' ? Number(v) : (v as number))
const nn = (v: unknown): number | null => (v == null ? null : n(v))

function copyOf(r: Record<string, unknown>): DupCopy {
  const cb: Record<string, number> = {}
  for (const [k, v] of Object.entries(r)) if (k.startsWith(CLASS_PREFIX) && v != null && n(v) > 0) cb[k.slice(CLASS_PREFIX.length)] = n(v)
  return {
    path: r.path as string,
    rel: r.rel as string,
    bucket: r.bucket as string,
    size: n(r.size),
    n_files: n(r.n_files),
    mtime: nn(r.mtime),
    created: nn(r.created),
    last_read: nn(r.last_read),
    cb,
    keep: !!r.keep,
    nested: !!r.nested,
    check: (r.check as Check | null) ?? null,
    check_n: nn(r.check_n),
    check_eq: nn(r.check_eq),
  }
}

function groupOf(r: Record<string, unknown>): DupGroup {
  return {
    group: r.group as string,
    tier: r.tier as Tier,
    rel: r.tier === 'moved' ? (r.rel as string).slice((r.rel as string).lastIndexOf('/') + 1) : r.rel as string,
    kind: r.kind as 'dir' | 'file',
    size: n(r.size),
    n_files: n(r.n_files),
    copies: n(r.copies),
    reclaimable: n(r.reclaimable),
    n_buckets: n(r.n_buckets),
    suspect: !!r.suspect,
    items: [],
  }
}

export interface DupsPage {
  groups: DupGroup[]
  /** More matching groups exist past the `n` returned. */
  more: boolean
  /** Rows decoded, and row groups read: the read's cost. */
  rows: number
  rowGroups: number
}

/** The first `limit` groups matching `f`, in the file's order (reclaimable bytes, descending). */
export async function readDups(file: AsyncBuffer, f: DupFilter, limit: number): Promise<DupsPage> {
  const metadata = await parquetMetadataAsync(file)
  const out: DupGroup[] = []
  let cur: DupGroup | null = null
  let rows = 0
  let rowGroups = 0
  let more = false
  let done = false
  // A finished group: kept when it matches; past `min`, nothing later can.
  const close = (g: DupGroup) => {
    if (f.min != null && g.reclaimable < f.min) { done = true; return }
    if (!groupMatches(g, f)) return
    if (out.length < limit) out.push(g)
    else { more = true; done = true }
  }
  let start = 0
  for (const rg of metadata.row_groups) {
    const end = start + n(rg.num_rows)
    const objs = (await parquetReadObjects({ file, metadata, rowStart: start, rowEnd: end, compressors })) as Record<string, unknown>[]
    start = end
    rows += objs.length
    rowGroups++
    for (const r of objs) {
      if (!cur || cur.group !== r.group) {
        if (cur) close(cur)
        if (done) break
        cur = groupOf(r)
      }
      cur.items.push(copyOf(r))
    }
    if (done) break
  }
  if (cur && !done) close(cur)
  return { groups: out, more, rows, rowGroups }
}

/** Plan items covering a copy: an open plan's prefix (`<scheme><bucket>/<path>/`) at or above it, or its exact
 *  object. */
export interface OpenItems {
  prefixes: Map<string, number>
  objects: Map<string, number>
}

/** The open plan whose item covers `path` (a dir copy: as a prefix; an object: exactly or under a prefix), or null.
 *  Probes the copy's `/`-cuts, so O(|path|). */
export function stagedPlan(path: string, kind: 'dir' | 'file', scheme: string, items: OpenItems): number | null {
  const key = `${scheme}${path}${kind === 'dir' ? '/' : ''}`
  if (kind === 'file') {
    const o = items.objects.get(key)
    if (o != null) return o
  }
  for (let i = key.indexOf('/', scheme.length); i >= 0; i = key.indexOf('/', i + 1)) {
    const p = items.prefixes.get(key.slice(0, i + 1))
    if (p != null) return p
  }
  return null
}
