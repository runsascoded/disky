// The children table's age bars (`TreeNode.ag`) from a scan's age pyramid, for views the interval store answers
// (its rows carry no `age_b*`: the strata are relative to the scan day, so every active dir's would change each
// scan). The pyramid's `1d` tier holds each path's dated bytes per created UTC day, the day `age_b<i>` buckets against
// the scan's, so its strata equal the per-scan columns exactly in every bucket but the oldest, which also holds the
// epoch-dated bytes the pyramid leaves out (`mtime` 0): that one is the node's bytes less the newer ones. (Exact where
// every object has a `created`, as a capture's: the per-scan columns leave one without out of every bucket.)
// A path under the pyramid's floor has no rows, so no bar.
import type { Env } from './auth.js'
import { num, openIndex, perScan, readAt, readUnder, str } from './index.js'

/** `AGE_EDGES_DAYS` (`cloud/src/dt_cloud/index.py`): `age_b<i>` holds the bytes created under `AGE_EDGES_DAYS[i]`
 *  days before the scan day (and at least the previous edge); the last bucket is everything older. */
export const AGE_EDGES_DAYS = [1, 7, 30, 91, 365, 1095]

const DAY_MS = 86_400_000

/** A scan id's UTC epoch day (`2026-10-18T1014` → its date's). */
export const scanDay = (date: string): number => Math.floor(Date.parse(`${date.slice(0, 10)}T00:00:00Z`) / DAY_MS)

/** The `age_b<i>` index of bytes `age` days old; a future day is the newest bucket, as `age_bucket_sql`. */
export const bucketOf = (age: number): number => {
  const i = AGE_EDGES_DAYS.findIndex(e => age < e)
  return i < 0 ? AGE_EDGES_DAYS.length : i
}

/** Each path's bytes by age bucket at `asofDay`, from `1d`-tier rows (`path`, `binstart` epoch ms, `b`). */
export function strata(rows: Record<string, unknown>[], asofDay: number): Map<string, number[]> {
  const out = new Map<string, number[]>()
  for (const r of rows) {
    const path = str(r.path)
    let ag = out.get(path)
    if (!ag) out.set(path, ag = new Array(AGE_EDGES_DAYS.length + 1).fill(0))
    ag[bucketOf(asofDay - Math.floor(num(r.binstart) / DAY_MS))] += num(r.b)
  }
  return out
}

/** The dated strata (pyramid rows only) of `path` itself (`node`, absent without a row) and of its children
 *  (`kids`, by name) at scan `date`, in two reads of the pyramid's `1d` tier. `null` when the scan has no pyramid, or
 *  the children span more row groups than one view may read: the view then carries no bars at all, never some. */
export async function datedAges(env: Env, date: string, path: string): Promise<{ node?: number[]; kids: Map<string, number[]> } | null> {
  const depth = path === '' ? 0 : path.split('/').length
  let h
  try {
    h = await openIndex(perScan(env), date, 'age-pyramid-1d')
  } catch (e) {
    if (/not synced/.test((e as Error).message)) return null
    throw e
  }
  const cols = ['path', 'depth', 'binstart', 'b']
  const [rows, own] = await Promise.all([readUnder(h, depth + 1, path, cols), readAt(h, depth, path, cols)])
  if (!rows || !own) return null
  const day = scanDay(date)
  const kids = new Map([...strata(rows, day)].map(([p, ag]) => [p.slice(p.lastIndexOf('/') + 1), ag]))
  return { node: strata(own, day).get(path), kids }
}

/** `b` bytes' strata from their dated ones: the oldest bucket is what the newer ones leave (dated or not). */
export const completed = (dated: number[], b: number): number[] => {
  const newer = dated.slice(0, -1)
  return [...newer, b - newer.reduce((t, x) => t + x, 0)]
}

/** A view's top node and its children with their bars from the pyramid, for an unscoped view the interval store
 *  answered (a lens, owner, class or query view's children are scoped bytes, which whole-path strata don't describe).
 *  `(other)` is the node's less the kept children's, when every kept child has one. */
export async function withChildAges<T extends { n: string; b: number; ag?: unknown; c?: T[] }>(env: Env, date: string, path: string, tree: T): Promise<T> {
  if (!tree.c?.length) return tree
  const got = await datedAges(env, date, path)
  if (!got) return tree
  const node = got.node && completed(got.node, tree.b)
  const c = tree.c.map(k => { const d = got.kids.get(k.n); return d ? { ...k, ag: completed(d, k.b) } : k })
  const kept = c.filter(k => k.n !== '(other)')
  const fold = node && kept.every(k => k.ag) ? node.map((x, i) => x - kept.reduce((t, k) => t + (k.ag as number[])[i], 0)) : undefined
  return { ...tree, ...(node ? { ag: node } : {}), c: c.map(k => (k.n === '(other)' && fold ? { ...k, ag: fold } : k)) }
}
