/** The build this page is (`scripts/build-id.mjs`, defined by `vite.config.ts`): the same commit and time the
 *  Functions answer with (`x-disky-build`), so a report can name the deploy both sides came from. */
export const BUILD_SHA: string = import.meta.env.VITE_BUILD_SHA ?? ''
export const BUILD_TIME: string = import.meta.env.VITE_BUILD_TIME ?? ''
export const SOURCE_REF: string = import.meta.env.VITE_SOURCE_REF ?? ''
export const BUILD_ENVIRONMENT: string = import.meta.env.VITE_BUILD_ENVIRONMENT ?? ''
export const REPO_URL = import.meta.env.VITE_REPO_URL || 'https://github.com/Open-Athena/disky'

/** GitHub's tree URL takes a ref (including slash-separated feature branches), while a commit link uses the repository base. */
export function sourceUrl(repo: string, ref: string): string {
  return `${repo.replace(/\/+$/, '')}${ref ? `/tree/${ref.split('/').map(encodeURIComponent).join('/')}` : ''}`
}
export const SOURCE_URL = sourceUrl(REPO_URL, SOURCE_REF)

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
