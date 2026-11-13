import type { Store } from '@rdub/file-tree'
import type { ReaderCache, ReaderObjects } from '../functions/_lib/readerRuntime.js'

/** Snapshot/extras reader over the same immutable object repository as the parquet reader. */
export function filesFromObjects(objects: ReaderObjects): Store {
  return {
    async get(key, range) {
      const object = await objects.get(key, range ? { range } : undefined)
      if (!object) throw Object.assign(new Error(`${key}: not found`), { name: 'NotFoundError' })
      return { bytes: new Uint8Array(await object.arrayBuffer()), totalSize: object.size }
    },
    async list(prefix, options) {
      if (options?.cursor) throw new Error('capsule file listing has no continuation')
      const entries = new Map<string, { key: string; isDir: boolean }>()
      let cursor: string | undefined
      do {
        const page = await objects.list({ prefix, cursor })
        for (const { key } of page.objects) {
          const slash = key.indexOf('/', prefix.length)
          const name = slash < 0 ? key : key.slice(0, slash + 1)
          entries.set(name, { key: name, isDir: slash >= 0 })
        }
        cursor = page.truncated ? page.cursor : undefined
      } while (cursor)
      return { entries: [...entries.values()].sort((a, b) => a.key < b.key ? -1 : a.key > b.key ? 1 : 0) }
    },
  }
}

/** Ordinary-process, size-bounded LRU; response bodies are owned bytes, never consumed Responses. */
export function memoryCache(cap = 16 * 1024 * 1024): ReaderCache {
  const held = new Map<string, { bytes: Uint8Array; headers: [string, string][]; status: number; until: number }>()
  let size = 0
  const keyOf = (key: RequestInfo | URL) => typeof key === 'string' ? key : key instanceof URL ? key.href : key.url
  const remove = (key: string) => { const prior = held.get(key); if (!prior) return false; size -= prior.bytes.byteLength; return held.delete(key) }
  return {
    async match(key) {
      const k = keyOf(key), value = held.get(k)
      if (!value) return undefined
      if (value.until <= Date.now()) { remove(k); return undefined }
      held.delete(k); held.set(k, value)
      return new Response(value.bytes.slice(), { headers: value.headers, status: value.status })
    },
    async put(key, response) {
      const k = keyOf(key), bytes = new Uint8Array(await response.arrayBuffer())
      remove(k)
      if (bytes.byteLength > cap) return
      const seconds = Number(/(?:s-maxage|max-age)=(\d+)/.exec(response.headers.get('cache-control') ?? '')?.[1] ?? 0)
      held.set(k, { bytes, headers: [...response.headers], status: response.status, until: Date.now() + seconds * 1000 })
      size += bytes.byteLength
      while (size > cap) remove(held.keys().next().value!)
    },
    delete: async key => remove(keyOf(key)),
  }
}
