/** The build this page is (`scripts/build-id.mjs`, defined by `vite.config.ts`): the same commit and time the
 *  Functions answer with (`x-disky-build`), so a report can name the deploy both sides came from. */
export const BUILD_SHA: string = import.meta.env.VITE_BUILD_SHA ?? ''
export const BUILD_TIME: string = import.meta.env.VITE_BUILD_TIME ?? ''

/** The ☰ menu's build line: the short sha (`-dirty` kept), the time as `YYYY-MM-DD HH:MM UTC`, and the commit's page
 *  under `repo` (none for a dirty or unknown build: it names no pushed commit). Null with no build id. */
export function buildLine(repo: string, sha = BUILD_SHA, time = BUILD_TIME): { sha: string; time: string; href: string | null } | null {
  if (!sha) return null
  const clean = /^[0-9a-f]{7,40}$/.test(sha)
  return {
    sha: clean ? sha.slice(0, 8) : sha,
    time: time.replace('T', ' ').replace(/:\d\dZ$/, ' UTC'),
    href: clean ? `${repo.replace(/\/+$/, '')}/commit/${sha}` : null,
  }
}
