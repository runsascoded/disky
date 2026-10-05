/** Warm on unfurl (specs/done/dogi.md): a card is fetched when a link is pasted,
 * usually just before someone clicks it. So serving a map card also fills
 * the edge cache with that view's first-paint reads: the `depth=1` subtree
 * and the full one, at the page's commonest canvas. Bounded: those two
 * requests, no retries. */

/** The canvas width a desktop window most often snaps to (`src/canvas.ts`
 * `WARMED_WIDTHS`: 1440–1536 px windows); the page asks `h = 0.6 w`. */
export const WARM_W = 1536

/** The first-paint subtree URLs (path + query) the map page would request
 * for a card's view, or none for kinds without one. Mirrors `App.tsx`'s
 * `scopeQs`: `f` → `q` (+ `qs`), a person in `o` → `lens=user:`, a pool or
 * `!` negation stays `o`, `cl` as is. `date` is the resolved scan. */
export function warmUrls(kind: string, params: Record<string, string>, date: string, canon: (u: string) => string = u => u): string[] {
  if (kind !== 'map' && kind !== 'user') return []
  const path = kind === 'user' ? '' : params.path ?? ''
  const o = kind === 'user' ? params.id : params.o
  const sp = new URLSearchParams({ date, path, w: String(WARM_W), h: String(Math.round(WARM_W * 0.6)) })
  if (o && o !== 'me') {
    if (o === 'owned' || o === 'unowned' || o.startsWith('!')) sp.set('o', o)
    else sp.set('lens', `user:${canon(o)}`)
  }
  if (params.cl) sp.set('cl', params.cl)
  if (params.f) { sp.set('q', params.f); if (params.qs) sp.set('qs', params.qs) }
  const base = `/api/subtree?${sp}`
  // A filtered page's first paint is the full read; an unfiltered one paints
  // `depth=1` first, then the full view.
  return params.f ? [`${base}&full=1`] : [`${base}&depth=1`, base]
}
