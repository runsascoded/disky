/** The build this deployment serves (`scripts/build-id.mjs`, baked into the bundle): its commit (`-dirty` when built
 *  from a modified checkout) and build time. Every Functions response carries it (`x-disky-build`, `_middleware.ts`),
 *  set as it is served — a cached body is the build's that stored it, the header is always the current one — so a
 *  bug report pins the deploy that answered (a not-yet-updated isolate included). `/api/health` names it too. */
import { BUILD } from './build.gen.js'
import { API_SHAPE, API_SHAPE_HEADER } from '../../src/apiShape.js'

export { BUILD }

export const BUILD_HEADER = 'x-disky-build'

/** The header's value: `<sha> <iso time>`, e.g. `8199b3c0ab5d2e0f61c9a4b7e3d18f20c6a9b5e7 2026-10-15T23:59:01Z` (the full sha, `scripts/build-id.mjs`). */
export const buildId = (b: { sha: string; time: string } = BUILD): string => `${b.sha} ${b.time}`

/** `res` with the build header and the API's wire shape (`x-disky-api`, `src/apiShape.ts`: a tab whose bundle names
 *  another reloads) set: in place, or on a copy when its headers are immutable (a `fetch` or Cache API response's are). */
export function withBuild(res: Response): Response {
  const id = buildId()
  const stamp = (h: Headers) => { h.set(BUILD_HEADER, id); h.set(API_SHAPE_HEADER, String(API_SHAPE)) }
  try {
    stamp(res.headers)
    return res
  } catch {
    const copy = new Response(res.body, res)
    stamp(copy.headers)
    return copy
  }
}
