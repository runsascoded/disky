/** The build this deployment serves (`scripts/build-id.mjs`, baked into the bundle): its commit (`-dirty` when built
 *  from a modified checkout) and build time. Every Functions response carries it (`x-disky-build`, `_middleware.ts`),
 *  set as it is served — a cached body is the build's that stored it, the header is always the current one — so a
 *  bug report pins the deploy that answered (a not-yet-updated isolate included). `/api/health` names it too. */
import { BUILD } from './build.gen.js'

export { BUILD }

export const BUILD_HEADER = 'x-disky-build'

/** The header's value: `<sha> <iso time>`, e.g. `8199b3c0ab 2026-10-15T23:59:01Z`. */
export const buildId = (b: { sha: string; time: string } = BUILD): string => `${b.sha} ${b.time}`

/** `res` with the build header set: in place, or on a copy when its headers are immutable (a `fetch` or Cache API
 *  response's are). */
export function withBuild(res: Response): Response {
  const id = buildId()
  try {
    res.headers.set(BUILD_HEADER, id)
    return res
  } catch {
    const copy = new Response(res.body, res)
    copy.headers.set(BUILD_HEADER, id)
    return copy
  }
}
