/** The share-link console's "Hide" field → path scopes (specs/path-scopes.md).
 *
 * One path per line: `/…` or store-relative, optionally led by a secondary
 * store's key (`nas:mnt/user/…`), or `~/…` on a store with a home dir
 * (`Store.home`). Each becomes `<tag>:hide:<prefix>`, the prefix
 * percent-encoded as `functions/_lib/restrict.ts` `hideScope` does (auth splits
 * stored scopes on spaces and commas). The primary's tag is its key, which is
 * its viewer scope.
 *
 * `unresolved`: `~` lines on a store with no home dir. They name no store path,
 * so they mint no scope, and the console refuses to mint while any remain
 * (a dropped line would leave its dir visible). */
export function hideScopes(text: string, primary: string, secondary: string[], home?: string[]): { scopes: string[]; unresolved: string[] } {
  const scopes: string[] = []
  const unresolved: string[] = []
  for (const raw of text.split('\n')) {
    let line = raw.trim()
    if (!line) continue
    let tag = primary
    const m = /^([a-z0-9][a-z0-9-]*):(.*)$/.exec(line)
    if (m && secondary.includes(m[1])) [tag, line] = [m[1], m[2].trim()]
    if (line === '~' || line.startsWith('~/')) {
      if (!home || tag !== primary) {
        unresolved.push(raw.trim())
        continue
      }
      line = [...home, line.slice(2)].join('/')
    }
    const prefix = line.replace(/^\/+|\/+$/g, '')
    if (!prefix) continue
    const scope = `${tag}:hide:${encodeURI(prefix).replace(/,/g, '%2C')}`
    if (!scopes.includes(scope)) scopes.push(scope)
  }
  return { scopes, unresolved }
}
