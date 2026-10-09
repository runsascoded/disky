/** The share-link console's "Hide" field → path scopes (specs/path-scopes.md).
 *
 * One path per line: `~/…` (the primary store's home), `/Users/…` or
 * store-relative, optionally led by a secondary store's key (`nas:mnt/user/…`).
 * Each becomes `<tag>:hide:<prefix>`, the prefix percent-encoded as
 * `functions/_lib/restrict.ts` `hideScope` does (auth splits stored scopes on
 * spaces and commas). The primary's tag is its key, which is its viewer scope. */
export function hideScopes(text: string, primary: string, secondary: string[], home: string[]): string[] {
  const out: string[] = []
  for (const raw of text.split('\n')) {
    let line = raw.trim()
    if (!line) continue
    let tag = primary
    const m = /^([a-z0-9][a-z0-9-]*):(.*)$/.exec(line)
    if (m && secondary.includes(m[1])) [tag, line] = [m[1], m[2].trim()]
    if (line === '~' || line.startsWith('~/')) line = [...home, line.slice(2)].join('/')
    const prefix = line.replace(/^\/+|\/+$/g, '')
    if (!prefix) continue
    const scope = `${tag}:hide:${encodeURI(prefix).replace(/,/g, '%2C')}`
    if (!out.includes(scope)) out.push(scope)
  }
  return out
}
