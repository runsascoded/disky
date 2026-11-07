/**
 * `GET /api/union-scans` — the union view a request reads (`_lib/union.ts`: `store=<view>`, or no `store=` on a
 * deployment with a main view) as the scan picker's sources: each member's mark (its view config's `label` /
 * `icon`, `stores.ts` `sources`; the label defaults to the member's `ROOT_LABEL`, else its key) and its indexed
 * scans at their instants (`observations`: the instants `latestObserved` resolves a cutoff against).
 *
 *   → { union: { view: "all", sources: [{ store: "primary", label: "Buckets", icon: "/icons/b.svg",
 *                                         scans: [{ id: "2026-10-14T0602", at: "2026-10-14T06:02:00.000Z" },
 *                                                 { id: "2026-10-13", at: "2026-10-13T00:00:00.000Z", day: true }, …] }, …] } }
 *   → { union: null }   no union view (a store, or a deployment without one): the single-store picker
 *
 * Scans newest first, per member. `day`: a date-only id with no recorded start, whose UTC midnight stands in for
 * its day (`scanInstant`) — the picker shows its date, not a time of day. Gated as every member's own read is
 * (`openUnion`).
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { excludedFromFunctions } from '../_lib/routes.js'
import { type UnionView, withStore } from '../_lib/stores.js'
import { openUnion, unionFor } from '../_lib/union.js'

const HEADERS = { 'cache-control': 'private, max-age=60' }

/** A view's config problems short of an error: an icon site path the Functions would answer (not excluded by
 *  `public/_routes.json`: the catch-all serves the SPA's `index.html` there, and the mark silently falls back to
 *  its label). An `http(s)://` icon is the deployment's own business. */
export function iconWarnings(view: UnionView): string[] {
  return Object.entries(view.sources).flatMap(([m, s]) => (s.icon?.startsWith('/') && !excludedFromFunctions(s.icon)
    ? [`STORES_JSON.${view.key}.sources.${m}.icon '${s.icon}': not excluded from the Functions (public/_routes.json), so it is served the SPA's index.html; put it under /icons/`]
    : []))
}

export const onRequestGet = async (ctx0: Ctx): Promise<Response> => {
  const view = unionFor(ctx0.request, ctx0.env)
  if (view instanceof Response) return view
  if (!view) {
    const ctx = withStore(ctx0)
    if (ctx instanceof Response) return ctx
    const gated = await requireViewer(ctx)
    if (gated instanceof Response) return gated
    return json({ union: null }, 200, HEADERS)
  }
  const u = await openUnion(ctx0, view, true)
  if (u instanceof Response) return u
  const sources = view.members.map((store, i) => {
    const shown = view.sources[store] ?? {}
    return {
      store,
      label: shown.label ?? u.ctxs[i].env.ROOT_LABEL ?? store,
      ...(shown.icon ? { icon: shown.icon } : {}),
      scans: [...u.obs[i]].sort((a, b) => (a.observed_through < b.observed_through ? 1 : a.observed_through > b.observed_through ? -1 : a.id < b.id ? 1 : -1))
        .map(o => ({ id: o.id, at: o.observed_through, ...(!o.id.includes('T') && o.observed_through === `${o.id}T00:00:00.000Z` ? { day: true } : {}) })),
    }
  })
  const warnings = iconWarnings(view)
  for (const w of warnings) console.warn(`union-scans: ${w}`)
  return json({ union: { view: view.key, sources, ...(warnings.length ? { warnings } : {}) } }, 200, HEADERS)
}
