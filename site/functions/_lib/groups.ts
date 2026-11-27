/** Bucket groups (specs/bucket-groups.md): a store's named sets of buckets (`STORES_JSON` `groups`, `stores.ts`),
 * which the Map shows together at `?g=<name>`.
 *
 * A group's view is the store's root restricted to its buckets: a synthetic root, labelled by the group, whose
 * children are its members. `g` acts at the root only (`path` empty); below it a request is an ordinary path read
 * and `g` is ignored, so no cache key below the root moves. At the root, each route answers by composition, like a
 * union view's root (`union.ts`): every member bucket's own read (`path=<bucket>`, no `g`) through the route's own
 * read path, under one byte threshold from the group's total, and the answers merged here. The members' reads are
 * cached as they always are (carrying `&T=` when read at the group's threshold); the group's merged answer isn't
 * cached itself.
 *
 * Nothing here names a deployment or a bucket: the config drives it.
 */
import { type Env, json } from './auth.js'
import { type Restriction, scopeReachesHidden } from './restrict.js'
import { type Flows, pointFlows, type SeriesPoint, type SeriesTrace, sumFlows } from './series.js'
import { type BucketGroup, StoreConfigError, storeConfigResponse, storeGroups } from './stores.js'
import type { DiffRow, DiffSides, ViewNode } from './view.js'

const NAME_RE = /^[a-z0-9][a-z0-9-]*$/

/** The request's `g=` as given; null = none. */
export function requestedGroup(request: Request): string | null {
  const g = new URL(request.url).searchParams.get('g')
  return g === null || g === '' ? null : g
}

/** Whether the request reads the store's root (`path` empty): where `g` acts. */
export const atRoot = (request: Request): boolean => (new URL(request.url).searchParams.get('path') ?? '').replace(/\/+$/, '') === ''

/** The group `name` names on the store `env` serves; a 400 (malformed), 404 (no such group) or 500 (a misconfigured
 *  `STORES_JSON`) otherwise. */
export function groupOf(env: Env, name: string): BucketGroup | Response {
  if (!NAME_RE.test(name)) return json({ error: 'bad group' }, 400)
  let gs: Record<string, BucketGroup>
  try {
    gs = storeGroups(env)
  } catch (e) {
    if (e instanceof StoreConfigError) return storeConfigResponse(e)
    throw e
  }
  return Object.hasOwn(gs, name) ? gs[name] : json({ error: `unknown group '${name}'` }, 404)
}

/** Whether a group holds something a path-restricted link hides: a member at or above a hidden dir. Its view is
 *  refused to such a link (`restrictedGroupRefused`), as an owner or class scope reaching a hidden dir is. */
export const groupReachesHidden = (g: BucketGroup, r: Restriction): boolean => g.buckets.some(b => scopeReachesHidden(b, r))

/** `request` as member `bucket`'s own read: `path=<bucket>`, no `g`, `set` params replaced and `drop` params removed.
 *  Headers (the session cookie), method and signal carry over. */
export function memberRequestOf(request: Request, bucket: string, set: Record<string, string> = {}, drop: readonly string[] = []): Request {
  const url = new URL(request.url)
  url.searchParams.delete('g')
  for (const k of drop) url.searchParams.delete(k)
  url.searchParams.set('path', bucket)
  for (const [k, v] of Object.entries(set)) url.searchParams.set(k, v)
  return new Request(url, request)
}

/** The group's view of itself (`group` in a root answer): its name, label and members. */
export const groupBlock = (g: BucketGroup): { name: string; label: string; buckets: string[] } => ({ name: g.name, label: g.label, buckets: g.buckets })

const byBytes = (x: ViewNode, y: ViewNode): number => y.bytes - x.bytes || (x.name < y.name ? -1 : x.name > y.name ? 1 : 0)

/** A group's root from its members' trees (each a bucket's read): the members largest first, under the group's label,
 *  with their sums — bytes, objects, the non-Standard classes' bytes, owners and unread ownership — and the newest
 *  write and read among them. */
export function groupRoot(label: string, kids: readonly ViewNode[]): ViewNode {
  const sum = (f: (n: ViewNode) => number | undefined) => kids.reduce((n, k) => n + (f(k) ?? 0), 0)
  const max = (f: (n: ViewNode) => number | undefined) => kids.reduce<number | undefined>((m, k) => { const v = f(k); return v === undefined || (m !== undefined && m >= v) ? m : v }, undefined)
  const classes = new Map<string, number>()
  const owners = new Map<string, number>()
  for (const k of kids) {
    for (const [c, b] of Object.entries(k.class_bytes ?? {})) classes.set(c, (classes.get(c) ?? 0) + b)
    for (const [u, b] of k.owners ?? []) owners.set(u, (owners.get(u) ?? 0) + b)
  }
  const mtime = max(k => k.mtime)
  const lastRead = max(k => k.last_read)
  const unknown = sum(k => k.unknown_owner_bytes)
  return {
    name: label, kind: 'dir',
    bytes: sum(k => k.bytes), objects: sum(k => k.objects),
    ...(mtime !== undefined ? { mtime } : {}),
    ...(lastRead !== undefined ? { last_read: lastRead } : {}),
    ...(classes.size ? { class_bytes: Object.fromEntries([...classes].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))) } : {}),
    ...(owners.size ? { owners: [...owners].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)) } : {}),
    ...(unknown ? { unknown_owner_bytes: unknown } : {}),
    ...(kids.length ? { children: [...kids].sort(byBytes) } : {}),
  }
}

/** A diff row's status from its two sides (`capDiffRows`' rule for an `(other)`: objects count when no bytes moved). */
export function sidesStatus(o: DiffSides): DiffRow['status'] {
  if (!o.bytes_before && !o.bytes_after) return o.objects_before === o.objects_after ? 'unchanged' : !o.objects_before ? 'added' : !o.objects_after ? 'removed' : 'changed'
  return !o.bytes_before ? 'added' : !o.bytes_after ? 'removed' : o.bytes_before !== o.bytes_after || o.objects_before !== o.objects_after ? 'changed' : 'unchanged'
}

/** A group root diff's rows from its members' diffs (each a bucket's, its rows relative to the bucket): each member a
 *  depth-1 row with its totals (`expanded` when its own rows came too), and its rows below it (`<bucket>/<row>`, one
 *  level deeper). Every member given gets its row, an unchanged one included: a group is an explicit set of buckets,
 *  and its totals count each member's bytes. */
export function groupDiffRows(members: readonly { bucket: string; total: DiffSides; rows: readonly DiffRow[] }[]): DiffRow[] {
  const out: DiffRow[] = []
  for (const { bucket, total, rows } of members) {
    const status = sidesStatus(total)
    out.push({
      path: bucket, depth: 1, kind: 'dir', status,
      bytes_before: total.bytes_before, bytes_after: total.bytes_after, objects_before: total.objects_before, objects_after: total.objects_after,
      ...(rows.length ? { expanded: true as const } : {}),
    })
    for (const r of rows) out.push({ ...r, path: `${bucket}/${r.path}`, depth: r.depth + 1 })
  }
  return out
}

/** A group's points from its members' series: per scan, the members' sums, over the scans any member has a point at
 *  (a member without one there adds nothing, as a root absent from a scan does). */
export function sumPoints(series: readonly (readonly SeriesPoint[])[]): SeriesPoint[] {
  const by = new Map<string, { date: string; bytes: number; objects: number; flows: (Flows | null)[] }>()
  for (const points of series) {
    for (const p of points) {
      const t = by.get(p.date) ?? { date: p.date, bytes: 0, objects: 0, flows: [] }
      t.bytes += p.bytes; t.objects += p.objects
      t.flows.push(pointFlows(p))
      by.set(p.date, t)
    }
  }
  // A scan's change flows: the members' sum, where each member with a point there has them (`sumFlows`).
  return [...by.values()].sort((a, b) => a.date.localeCompare(b.date)).map(({ flows, ...t }) => ({ ...t, ...(sumFlows(flows) ?? {}) }))
}

/** The members' traces of a `split=roots` answer, in the group's order (a member no scan has is absent). */
export const memberTraces = (g: BucketGroup, roots: readonly SeriesTrace[]): SeriesTrace[] =>
  g.buckets.flatMap(b => roots.filter(t => t.path === b))
