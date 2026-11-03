/**
 * Index extras: the provenance of inferred ownership (every attributing prefix → user, source, evidence).
 *
 * Where the deployment's interval store carries the attribution table and it covers the scan
 * (specs/attr-interval-table.md), it answers from there (`ivAttribution`: the table as of the scan, the view's subtree
 * and its ancestors). Otherwise from the per-scan sidecar beside the index tiers — `attr.tsv`, sorted by key with a
 * block index (`attr.tsv.idx.json`), so a view fetches only the byte range covering its subtree (keys under `P` are
 * contiguous in sorted order) plus the blocks holding P's ancestors. The sidecar is found through the scans' layout
 * (`SIDECAR_LAYOUTS`: the newest index generation dir holding one), never through D1, so it is still found once the
 * scan's D1 pointer is retired (`scan retire -P`). Its index is memoized per scan per isolate; a scan neither the table
 * nor a sidecar covers yields `null` and every consumer keeps today's behaviour.
 */
import type { Env } from './auth.js'
import { ivAttributes, ivAttribution, servedStore } from './index.js'
import { joinOf, shared } from './shared.js'
import { storeKey } from './stores.js'

export type Provenance = [source: string, evidence: string | null, prefix: string]

export interface BlockIndex { v: number; n: number; size: number; keys: string[]; offsets: number[] }

/** The most a view will pull of one sidecar: past this (a bucket root over a
 *  sidecar with hundreds of thousands of lines) the extras are skipped for
 *  that view rather than parsed whole. */
const MAX_RANGE = 8 * 1024 * 1024

/** Where a scan's sidecar lives, newest first: under each scan's index generation dirs (`{gen}`: the newest holding
 *  one wins), then the scan's listing dir itself (the oldest layout). */
export const SIDECAR_LAYOUTS = ['listing/{id}/index/{gen}', 'listing/{id}']

export interface ExtrasView {
  /** The provenance of `user`'s bytes under `path`: the deepest attributing
   *  ancestor-or-self whose user is `user`, or null. */
  provenance(path: string, user: string): Provenance | null
}

const idxMemo = new Map<string, Promise<{ dir: string; attr: BlockIndex | null } | null>>()
// A scan WITHOUT sidecars is re-probed after a minute, so a backfill shows
// up promptly rather than after the isolate's next recycle. Tracked by
// timestamp, not a timer: a `setTimeout` armed inside a request can be
// dropped once that request's context ends.
const MISS_TTL_MS = 60_000
const missAt = new Map<string, number>()

/** The sidecars' store: the served seam (`INDEX_R2` under `SERVED_R2`), allowed only the scan's listing dir. */
const sidecars = (env: Env, date: string) => servedStore(env, [`listing/${date}/`])

async function readJson(env: Env, date: string, key: string): Promise<unknown | null> {
  try {
    const { bytes } = await sidecars(env, date).get(key)
    return JSON.parse(new TextDecoder().decode(bytes))
  } catch {
    return null
  }
}

/** The dirs `SIDECAR_LAYOUTS` name for scan `date`, in the order they're tried: each `{gen}` layout's generation dirs
 *  (listed), newest first, then the fixed ones. */
async function sidecarDirs(env: Env, date: string): Promise<string[]> {
  const out: string[] = []
  for (const layout of SIDECAR_LAYOUTS) {
    const t = layout.replace('{id}', date)
    if (!t.includes('{gen}')) {
      out.push(t)
      continue
    }
    const root = t.slice(0, t.indexOf('{gen}'))
    const gens: string[] = []
    try {
      let cursor: string | undefined
      do {
        const page = await sidecars(env, date).list(root, cursor ? { cursor } : undefined)
        for (const e of page.entries) if (e.isDir) gens.push(e.key.replace(/\/$/, ''))
        cursor = page.cursor
      } while (cursor)
    } catch {
      continue
    }
    out.push(...gens.sort().reverse())
  }
  return out
}

/** The scan's sidecar index (or null when no layout dir holds one). */
async function indexes(env: Env, date: string) {
  const key = `${storeKey(env)}|${date}`
  const miss = missAt.get(key)
  if (miss != null && Date.now() - miss > MISS_TTL_MS) { idxMemo.delete(key); missAt.delete(key) }
  const got = await shared(idxMemo, key, async () => {
    for (const dir of await sidecarDirs(env, date)) {
      const attr = await readJson(env, date, `${dir}/attr.tsv.idx.json`)
      if (attr) return { dir, attr: attr as BlockIndex | null }
    }
    return null
  }, 30_000, joinOf(env, 'extras'))
  if (!got) missAt.set(key, missAt.get(key) ?? Date.now())
  else missAt.delete(key)
  return got
}

/** Whether the scan has extras (the store's table covers it, or a sidecar) — for cache keys, so a backfill isn't
 *  hidden behind responses cached before it landed. */
export async function hasExtras(env: Env, date: string): Promise<boolean> {
  return (await ivAttributes(env, date)) || (await indexes(env, date)) != null
}

/** Index of the block whose first key ≤ `key` (−1 before the first). */
export function blockOf(idx: BlockIndex, key: string): number {
  let lo = 0
  let hi = idx.keys.length - 1
  let ans = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (idx.keys[mid] <= key) { ans = mid; lo = mid + 1 } else hi = mid - 1
  }
  return ans
}

/** The byte span `[start, end)` covering blocks `from..to` (inclusive),
 *  clamped to the index; null when the range is empty — `to < from`, or
 *  `from` past the last block (an empty sidecar has zero blocks, so *every*
 *  range is empty). Guards the reader from fabricating a `[undefined, 0]`
 *  range out of an empty `offsets`, which becomes a malformed store GET. */
export function blockSpan(idx: BlockIndex, from: number, to: number): [number, number] | null {
  if (from < 0) from = 0
  if (to < from || from >= idx.offsets.length) return null
  const start = idx.offsets[from]
  const end = to + 1 < idx.offsets.length ? idx.offsets[to + 1] : idx.size
  return end <= start ? null : [start, end]
}

/** The lines of blocks `from..to` (inclusive), or null when over budget. */
async function readBlocks(env: Env, date: string, dir: string, idx: BlockIndex, from: number, to: number): Promise<string[] | null> {
  const span = blockSpan(idx, from, to)
  if (!span) return []
  const [start, end] = span
  if (end - start > MAX_RANGE) return null
  const { bytes } = await sidecars(env, date).get(`${dir}/attr.tsv`, { offset: start, length: end - start })
  const text = new TextDecoder().decode(bytes)
  return text.split('\n').filter(Boolean)
}

/** Every key in `[lo, hi)`, via the block index. */
async function rangeLines(env: Env, date: string, dir: string, idx: BlockIndex, lo: string, hi: string): Promise<string[] | null> {
  const a = blockOf(idx, lo)
  const b = blockOf(idx, hi)
  return readBlocks(env, date, dir, idx, a, b < 0 ? 0 : b)
}

const ancestorsOf = (path: string): string[] => {
  const out: string[] = []
  for (let p = path; p; p = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '') out.push(p)
  return out
}

/**
 * The extras a view rooted at `path` needs: an attribution map covering its
 * subtree plus its ancestors. Null when neither the store's table nor a sidecar
 * covers the scan, or the table's read is too wide.
 */
export async function extrasFor(env: Env, date: string, path: string): Promise<ExtrasView | null> {
  const iv = await ivAttribution(env, date, path)
  if (iv !== undefined) return iv && viewOfRows(iv.map(r => [r.path, r.user, r.source, r.evidence]))
  const ix = await indexes(env, date)
  if (!ix) return null
  const lo = path
  const hi = path === '' ? '￿' : path + '0' // '0' sorts just past '/'
  const [attrLines, ancLines] = await Promise.all([
    ix.attr ? rangeLines(env, date, ix.dir, ix.attr, lo, hi) : Promise.resolve([]),
    // The root's ancestors: one block each (deduped), for inherited provenance.
    ix.attr
      ? Promise.all([...new Set(ancestorsOf(path).slice(1).map(a => blockOf(ix.attr!, a)))].map(b => readBlocks(env, date, ix.dir, ix.attr!, b, b)))
      : Promise.resolve([]),
  ])
  return viewOf([...(attrLines ?? []), ...ancLines.flatMap(l => l ?? [])])
}

/** An attributing prefix: `[path, user, source, evidence]`. */
export type AttrRow = [path: string, user: string | null, source: string | null, evidence: string | null]

/** An `ExtrasView` over already-fetched sidecar lines (the pure half, for tests). */
export function viewOf(attrLines: string[]): ExtrasView {
  const rows: AttrRow[] = []
  for (const line of attrLines) {
    const [k, u, src, ev] = line.split('\t')
    if (k && u) rows.push([k, u, src || null, ev || null])
  }
  return viewOfRows(rows)
}

/** An `ExtrasView` over attributing prefixes (the store's table, or a sidecar's lines). */
export function viewOfRows(rows: AttrRow[]): ExtrasView {
  const attr = new Map<string, [string | null, string | null, string | null]>()
  for (const [k, u, src, ev] of rows) attr.set(k, [u, src, ev])
  const provenance = (p: string, user: string): Provenance | null => {
    for (const a of ancestorsOf(p)) {
      const hit = attr.get(a)
      if (hit && hit[0] === user) return [hit[1] ?? 'unknown', hit[2], a]
    }
    return null
  }
  return { provenance }
}
