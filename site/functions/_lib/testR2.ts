/** Test-only: an `R2Bucket` (`get` with ranges, `list`, `head`) over a fixture dir (`fixtures/<dir>/`), each `get`'s key
 *  pushed to `reads`. */
import { fixture } from './testStore.js'

export function fixtureR2(dir: string, reads: string[]): R2Bucket {
  type Fs = { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array; readdirSync(p: string, o: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean } }
  const nodeFs = async () => {
    const mod = 'node:fs'
    return (await import(/* @vite-ignore */ mod)) as Fs
  }
  const path = (key: string) => fixture(`${dir}/${key}`)
  const list = async ({ prefix }: { prefix: string }) => {
    const fs = await nodeFs()
    const d = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = path(d)
    const keys = fs.existsSync(root) ? fs.readdirSync(root, { recursive: true }).map(f => `${d}${f}`).filter(k => k.startsWith(prefix) && fs.statSync(path(k)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), truncated: false }
  }
  const get = async (key: string, o?: { range?: { offset: number; length: number } }) => {
    const fs = await nodeFs()
    if (!fs.existsSync(path(key))) return null
    const buf = fs.readFileSync(path(key))
    const { offset = 0, length = buf.byteLength - offset } = o?.range ?? {}
    reads.push(key)
    const part = buf.slice(offset, offset + length)
    return {
      size: buf.byteLength,
      arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength),
      json: async () => JSON.parse(new TextDecoder().decode(part)),
    }
  }
  const head = async (key: string) => ((await nodeFs()).existsSync(path(key)) ? { key } : null)
  return { get, list, head } as unknown as R2Bucket
}

/** An `R2Bucket`'s `list` / `get` over a map of key → text, enough for `R2Store` (pages of 2 keys, so listings page). A key
 *  in `broken` fails its `get`. */
export function fakeR2(files: Record<string, string>, broken: readonly string[] = []) {
  return {
    async list({ prefix, cursor, limit }: { prefix: string; cursor?: string; limit: number }) {
      const keys = Object.keys(files).filter(k => k.startsWith(prefix)).sort()
      const at = cursor ? Number(cursor) : 0
      const page = keys.slice(at, at + Math.min(limit, 2))
      const more = at + page.length < keys.length
      return {
        objects: page.map(key => ({ key, size: files[key].length, uploaded: new Date(0) })),
        delimitedPrefixes: [], truncated: more, cursor: more ? String(at + page.length) : undefined,
      }
    },
    async get(key: string) {
      if (broken.includes(key)) throw new Error(`r2 get failed: ${key}`)
      const v = files[key]
      return v === undefined ? null : { size: v.length, arrayBuffer: async () => new TextEncoder().encode(v).buffer }
    },
  }
}

/** The job's soft-delete reading for a day (`snapshots/soft-delete/<date>.json`, read at 07:12:03Z): `{ bucket: off }`, an
 *  on bucket retaining 7 days. */
export const softDeleteDoc = (date: string, off: Record<string, boolean>, version = 1): string => JSON.stringify({
  version, date, as_of: `${date}T07:12:03Z`, buckets: Object.fromEntries(Object.entries(off).map(([bucket, isOff]) => [bucket, { retention_seconds: isOff ? 0 : 604800 }])),
})
