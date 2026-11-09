/** Ordinary Node adapter: real filesystem input + node:sqlite, without Cloudflare globals or bindings. */
import { createHash } from 'node:crypto'
import { open } from 'node:fs/promises'
import { resolve, relative, isAbsolute } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import type { ReaderMetadata, ReaderObjects, ReaderStatement, ReaderServices } from '../functions/_lib/readerRuntime.js'
import { createExecutor, type Capsule } from './kernel.js'
import { filesFromObjects, memoryCache } from './stores.js'

export interface CapsuleFile {
  capsule: Capsule
  sql: string
  objects: { key: string; file: string; sha256: string }[]
}
export const sha256 = (bytes: Uint8Array | string): string => createHash('sha256').update(bytes).digest('hex')

export function sqliteMetadata(sql: string): ReaderMetadata {
  const db = new DatabaseSync(':memory:')
  db.exec(sql)
  const step = new WeakMap<ReaderStatement, () => unknown>()
  const statement = (query: string, parameters: unknown[] = []): ReaderStatement => {
    const raw = db.prepare(query)
    const values = parameters as (string | number | null | Uint8Array)[]
    const result: ReaderStatement = {
      bind: (...args) => statement(query, args),
      first: async <T>() => (raw.get(...values) ?? null) as T | null,
      all: async <T>() => ({ results: raw.all(...values) as T[] }),
      run: async () => raw.run(...values),
    }
    step.set(result, () => raw.run(...values))
    return result
  }
  return {
    prepare: query => statement(query),
    async batch(statements) {
      db.exec('BEGIN')
      try {
        for (const statement of statements) {
          const execute = step.get(statement)
          if (!execute) throw new Error('statement belongs to another metadata adapter')
          execute()
        }
        db.exec('COMMIT')
      } catch (error) { db.exec('ROLLBACK'); throw error }
    },
  }
}

/** Small correctness capsules are verified and copied into owned bytes before any request. Later filesystem
 * changes cannot change the pinned input. This bounded fixture adapter is not a production filesystem cache. */
export async function readBounded(path: string, cap: number): Promise<Uint8Array> {
  const file = await open(path, 'r')
  try {
    const stat = await file.stat()
    if (!stat.isFile() || stat.size > cap) throw new Error('capsule exceeds its input-byte cap')
    // The extra byte detects growth after stat; allocation never exceeds the remaining admission cap + 1.
    const bytes = new Uint8Array(stat.size + 1)
    let offset = 0
    while (offset < bytes.length) {
      const result = await file.read(bytes, offset, bytes.length - offset, offset)
      if (!result.bytesRead) break
      offset += result.bytesRead
    }
    if (offset > cap) throw new Error('capsule exceeds its input-byte cap')
    if (offset !== stat.size) throw new Error('capsule object changed while reading')
    return bytes.slice(0, offset)
  } finally { await file.close() }
}

export async function nodeExecutor(document: CapsuleFile, root: string, maxBytes = 16 * 1024 * 1024) {
  const content = new Map<string, Uint8Array>()
  let total = 0
  for (const object of document.objects) {
    const path = resolve(root, object.file), rel = relative(resolve(root), path)
    if (rel.startsWith('..') || isAbsolute(rel)) throw new Error('capsule path escapes root')
    if (content.has(object.key)) throw new Error(`duplicate capsule key ${object.key}`)
    const bytes = await readBounded(path, maxBytes - total)
    total += bytes.byteLength
    if (total > maxBytes) throw new Error('capsule exceeds its input-byte cap')
    if (sha256(bytes) !== object.sha256) throw new Error(`capsule hash mismatch: ${object.key}`)
    content.set(object.key, Uint8Array.from(bytes))
  }
  const { id, ...identity } = document.capsule
  const actual = sha256(JSON.stringify({ capsule: identity, sql: document.sql, objects: document.objects }))
  if (id !== actual) throw new Error('capsule identity mismatch')
  const objects: ReaderObjects = {
    async get(key, options) {
      const all = content.get(key)
      if (!all) return null
      const range = options?.range
      const offset = range && 'suffix' in range ? Math.max(0, all.length - range.suffix) : range?.offset ?? 0
      const length = range && 'length' in range ? range.length ?? all.length - offset : all.length - offset
      const bytes = all.slice(offset, offset + length)
      return { size: all.length, arrayBuffer: async () => bytes.buffer, json: async <T>() => JSON.parse(new TextDecoder().decode(bytes)) as T }
    },
    head: async key => content.has(key) ? { size: content.get(key)!.length } : null,
    list: async ({ prefix, cursor }) => {
      if (cursor) throw new Error('capsule object listing has no continuation')
      return { objects: [...content.keys()].filter(k => k.startsWith(prefix)).sort().map(key => ({ key })), truncated: false }
    },
  }
  const services: ReaderServices = { objects, files: filesFromObjects(objects), metadata: sqliteMetadata(document.sql), cache: memoryCache() }
  return createExecutor(document.capsule, services)
}
