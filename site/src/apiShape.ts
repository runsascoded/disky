/** The site API's wire shape: bumped with any change a running tab's code can't read (a field renamed or dropped, a
 *  body restructured), never for additions. Every Functions response names the server's (`x-disky-api`,
 *  `functions/_lib/build.ts` `withBuild`), and a tab whose bundle names another reloads once (`watchApiShape`): a tab
 *  left open across such a deploy otherwise reads bodies it can't parse (2026-11-13: a tab from before the
 *  descriptive field names crashed on `/meta`, sorting nodes by a field the server no longer sent).
 *  1: the short keys (`n k b o …`); 2: the descriptive names (`specs/descriptive-field-names.md`). */
export const API_SHAPE = 2

export const API_SHAPE_HEADER = 'x-disky-api'

/** Whether a response's shape header names a shape other than this bundle's (absent: an older server, or not ours). */
export const shapeSkewed = (header: string | null, mine: number = API_SHAPE): boolean =>
  header != null && header.trim() !== '' && header.trim() !== String(mine)

/** The once-per-shape reload's `sessionStorage` key: a reload that still meets the same other shape (a stale cached
 *  bundle) doesn't loop. */
export const reloadKey = (theirs: string): string => `disky-api-shape-reload:${theirs.trim()}`

interface Win {
  fetch: typeof fetch
  location: { reload(): void }
}
interface Store {
  getItem(k: string): string | null
  setItem(k: string, v: string): void
}

/** Wrap `win.fetch` so a response naming another API shape reloads the page, once per shape per tab session. Storage
 *  that throws (a private window) means no reload rather than a loop. */
export function watchApiShape(win: Win = globalThis as unknown as Win, store: Store | null = safeSession()): void {
  const inner = win.fetch.bind(win)
  win.fetch = async (...args: Parameters<typeof fetch>) => {
    const res = await inner(...args)
    const theirs = res.headers.get(API_SHAPE_HEADER)
    if (store && shapeSkewed(theirs)) {
      try {
        const key = reloadKey(theirs!)
        if (!store.getItem(key)) {
          store.setItem(key, '1')
          win.location.reload()
        }
      } catch { /* no storage: no reload */ }
    }
    return res
  }
}

function safeSession(): Store | null {
  try {
    return (globalThis as { sessionStorage?: Store }).sessionStorage ?? null
  } catch {
    return null
  }
}
