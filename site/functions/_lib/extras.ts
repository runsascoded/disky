/**
 * Index extras: the per-scan provenance sidecar beside the index tiers —
 * `attr.tsv` (every attributing prefix → user, source, evidence), sorted by
 * key with a block index (`attr.tsv.idx.json`), so a view fetches only the
 * byte range covering its subtree (keys under `P` are contiguous in sorted
 * order) plus the blocks holding P's ancestors. The index is memoized per
 * (scan, generation) per isolate; a scan without the sidecar (older
 * generations, until `dt-cloud index-extras` backfills) yields `null` and
 * every consumer keeps today's behaviour.
 */
import type { Env } from './auth.js'
import { makeStore, schemaRow } from './index.js'
import { shared } from './shared.js'
import { storeKey } from './stores.js'

export type Provenance = [source: string, evidence: string | null, prefix: string]

export interface BlockIndex { v: number; n: number; size: number; keys: string[]; offsets: number[] }

/** The most a view will pull of one sidecar: past this (a bucket root over a
 *  sidecar with hundreds of thousands of lines) the extras are skipped for
 *  that view rather than parsed whole. */
const MAX_RANGE = 8 * 1024 * 1024

export interface ExtrasView {
  /** The provenance of `user`'s bytes under `path`: the deepest attributing
   *  ancestor-or-self whose user is `user`, or null. */
  provenance(path: string, user: string): Provenance | null
}

const idxMemo = new Map<string, Promise<{ dir: string; attr: BlockIndex | null } | null>>()
const TTL_MS = 10 * 60_000
// A scan WITHOUT sidecars is re-probed after a minute, so a backfill shows
// up promptly rather than after the isolate's next recycle. Tracked by
// timestamp, not a timer: a `setTimeout` armed inside a request can be
// dropped once that request's context ends.
const MISS_TTL_MS = 60_000
const missAt = new Map<string, number>()

async function readJson(env: Env, key: string): Promise<unknown | null> {
  try {
    const { bytes } = await makeStore(env).get(key)
    return JSON.parse(new TextDecoder().decode(bytes))
  } catch {
    return null
  }
}

/** The scan's sidecar indexes (or null when the generation has none). */
async function indexes(env: Env, date: string) {
  if (!env.DB) return null
  const r = await schemaRow<{ dir: string | null }>(env, 'dir', date, 'path')
  const dir = r?.dir ?? `listing/${date}`
  const key = `${storeKey(env)}|${date}|${dir}`
  const miss = missAt.get(key)
  if (miss != null && Date.now() - miss > MISS_TTL_MS) { idxMemo.delete(key); missAt.delete(key) }
  const got = await shared(idxMemo, key, async () => {
    const attr = await readJson(env, `${dir}/attr.tsv.idx.json`)
    if (!attr) return null
    return { dir, attr: attr as BlockIndex | null }
  }, TTL_MS)
  if (!got) missAt.set(key, missAt.get(key) ?? Date.now())
  else missAt.delete(key)
  return got
}

/** Whether the scan has sidecars — for cache keys, so a backfill isn't
 *  hidden behind responses cached before it landed. */
export async function hasExtras(env: Env, date: string): Promise<boolean> {
  return (await indexes(env, date)) != null
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
async function readBlocks(env: Env, key: string, idx: BlockIndex, from: number, to: number): Promise<string[] | null> {
  const span = blockSpan(idx, from, to)
  if (!span) return []
  const [start, end] = span
  if (end - start > MAX_RANGE) return null
  const { bytes } = await makeStore(env).get(key, { offset: start, length: end - start })
  const text = new TextDecoder().decode(bytes)
  return text.split('\n').filter(Boolean)
}

/** Every key in `[lo, hi)`, via the block index. */
async function rangeLines(env: Env, key: string, idx: BlockIndex, lo: string, hi: string): Promise<string[] | null> {
  const a = blockOf(idx, lo)
  const b = blockOf(idx, hi)
  return readBlocks(env, key, idx, a, b < 0 ? 0 : b)
}

const ancestorsOf = (path: string): string[] => {
  const out: string[] = []
  for (let p = path; p; p = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '') out.push(p)
  return out
}

/**
 * The extras a view rooted at `path` needs: an attribution map covering its
 * subtree plus its ancestors. Null when the scan has no sidecar.
 */
export async function extrasFor(env: Env, date: string, path: string): Promise<ExtrasView | null> {
  const ix = await indexes(env, date)
  if (!ix) return null
  const lo = path
  const hi = path === '' ? '￿' : path + '0' // '0' sorts just past '/'
  const [attrLines, ancLines] = await Promise.all([
    ix.attr ? rangeLines(env, `${ix.dir}/attr.tsv`, ix.attr, lo, hi) : Promise.resolve([]),
    // The root's ancestors: one block each (deduped), for inherited provenance.
    ix.attr
      ? Promise.all([...new Set(ancestorsOf(path).slice(1).map(a => blockOf(ix.attr!, a)))].map(b => readBlocks(env, `${ix.dir}/attr.tsv`, ix.attr!, b, b)))
      : Promise.resolve([]),
  ])
  return viewOf([...(attrLines ?? []), ...ancLines.flatMap(l => l ?? [])])
}

/** An `ExtrasView` over already-fetched lines (the pure half, for tests). */
export function viewOf(attrLines: string[]): ExtrasView {
  const attr = new Map<string, [string, string | null, string | null]>()
  for (const line of attrLines) {
    const [k, u, src, ev] = line.split('\t')
    if (k && u) attr.set(k, [u, src || null, ev || null])
  }
  const provenance = (p: string, user: string): Provenance | null => {
    for (const a of ancestorsOf(p)) {
      const hit = attr.get(a)
      if (hit && hit[0] === user) return [hit[1] ?? 'unknown', hit[2], a]
    }
    return null
  }
  return { provenance }
}
