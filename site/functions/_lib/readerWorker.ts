/** Existing Workers bindings adapted to the reader's portable I/O contract. */
import { readerServices, type ReaderMetadata, type ReaderObjects, type ReaderStatement } from './readerRuntime.js'

const databases = new WeakMap<D1Database, ReaderMetadata>()
export function readerMetadata(env: { DB?: D1Database }): ReaderMetadata | undefined {
  const supplied = readerServices(env)
  if (supplied) return supplied.metadata
  const db = env.DB
  if (!db) return undefined
  let adapter = databases.get(db)
  if (!adapter) {
    const native = new WeakMap<ReaderStatement, D1PreparedStatement>()
    const statement = (s: D1PreparedStatement): ReaderStatement => {
      const wrapped: ReaderStatement = {
        bind: (...args) => statement(s.bind(...args)),
        first: () => s.first(),
        all: () => s.all(),
        run: () => s.run(),
      }
      native.set(wrapped, s)
      return wrapped
    }
    adapter = {
      prepare: sql => statement(db.prepare(sql)),
      batch: statements => db.batch(statements.map(s => {
        const raw = native.get(s)
        if (!raw) throw new Error('reader statement belongs to another metadata adapter')
        return raw
      })),
    }
    databases.set(db, adapter)
  }
  return adapter
}

/** R2 already implements these read operations; only its binding-specific types stay here. */
export function readerObjects(env: { INDEX_R2?: R2Bucket }): ReaderObjects | undefined {
  return readerServices(env)?.objects ?? env.INDEX_R2
}
