/**
 * `GET /api/filter-scans` — the scans the static name index covers (`staticFilter.ts`'s store, the same list
 * `indexedGate` checks), so a filtered view with no explicit `?d=` can open on the newest of them before it
 * fetches anything, instead of on a just-published scan the index hasn't appended yet. `store=<key>` answers
 * for that store's own index (none of its own, under the static filter: `[]`).
 *
 *   → { scans: ["2026-10-08", "2026-10-09"] }   ascending; null when the static filter is off
 *
 * A union view (`_lib/union.ts`): `scans` are the cutoffs (scans any member indexes) at which every member's
 * resolved scan is in its own index (a member with the static filter off, or no scan by the cutoff, never
 * blocks one); `union.sources` lists each member's own index's scans (null: its static filter is off).
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { staticFilterStore, type StaticFilterEnv } from '../_lib/staticFilter.js'
import { type UnionView, withStore } from '../_lib/stores.js'
import { membersAt, openUnion, unionFor } from '../_lib/union.js'

const HEADERS = { 'cache-control': 'private, max-age=60' }

export const onRequestGet = async (ctx0: Ctx): Promise<Response> => {
  const u = unionFor(ctx0.request, ctx0.env)
  if (u instanceof Response) return u
  if (u) return unionScans(ctx0, u)
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const s = staticFilterStore(ctx.env as unknown as StaticFilterEnv)
  const scans = s ? [...await s.scans()].sort() : null
  return json({ scans }, 200, HEADERS)
}

async function unionScans(ctx0: Ctx, view: UnionView): Promise<Response> {
  const u = await openUnion(ctx0, view, true)
  if (u instanceof Response) return u
  const held = await Promise.all(u.ctxs.map(async c => {
    const s = staticFilterStore(c.env as unknown as StaticFilterEnv)
    return s ? [...await s.scans()].sort() : null
  }))
  const sources = view.members.map((store, i) => ({ store, scans: held[i] }))
  if (held.every(h => h === null)) return json({ scans: null, union: { view: view.key, sources } }, 200, HEADERS)
  const cutoffs = new Map(u.obs.flat().map(o => [o.id, o.observed_through]))
  const scans = [...cutoffs].filter(([date, at]) => membersAt(u, { date, at }).members.every((m, i) => m.date === null || held[i] === null || held[i]!.includes(m.date))).map(([date]) => date).sort()
  return json({ scans, union: { view: view.key, sources } }, 200, HEADERS)
}
