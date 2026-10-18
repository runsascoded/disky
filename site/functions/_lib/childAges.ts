// The children table's age bars (`TreeNode.ag`) from a scan's age pyramid, for views the interval store answers
// (its rows carry no `age_b*`: the strata are relative to the scan day, so every active dir's would change each
// scan). The pyramid's `1d` tier holds each path's bytes per created UTC day, the day `age_b<i>` buckets against the
// scan's, so the strata it gives equal the per-scan columns exactly. A path under the pyramid's floor has no rows,
// so no bar.
import type { Env } from './auth.js'
import { num, openIndex, perScan, readUnder, str } from './index.js'

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

/** `path`'s children's strata at scan `date`, keyed by child name, in one read of the pyramid's `1d` tier. `null`
 *  when the scan has no pyramid, or its children span more row groups than one view may read: the view then
 *  carries no bars at all, never some. */
export async function childAges(env: Env, date: string, path: string): Promise<Map<string, number[]> | null> {
  const depth = path === '' ? 1 : path.split('/').length + 1
  let h
  try {
    h = await openIndex(perScan(env), date, 'age-pyramid-1d')
  } catch (e) {
    if (/not synced/.test((e as Error).message)) return null
    throw e
  }
  const rows = await readUnder(h, depth, path, ['path', 'depth', 'binstart', 'b'])
  if (!rows) return null
  const byPath = strata(rows, scanDay(date))
  return new Map([...byPath].map(([p, ag]) => [p.slice(p.lastIndexOf('/') + 1), ag]))
}

/** A view's top node with its children's bars set from the pyramid, for an unscoped view the interval store answered
 *  (a lens, owner, class or query view's children are scoped bytes, which whole-path strata don't describe). */
export async function withChildAges<T extends { n: string; c?: T[] } & Record<string, unknown>>(env: Env, date: string, path: string, tree: T): Promise<T> {
  if (!tree.c?.length) return tree
  const ages = await childAges(env, date, path)
  if (!ages) return tree
  return { ...tree, c: tree.c.map(k => (ages.has(k.n) ? { ...k, ag: ages.get(k.n) } : k)) }
}
