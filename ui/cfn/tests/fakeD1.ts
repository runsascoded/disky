/** Just enough of `D1Database` for the Functions, over `node:sqlite` in memory:
 *  numbered-param `prepare().bind().first()/all()/run()` and `batch()`, plus a
 *  helper that applies the repo's `migrations/*.sql` so a test DB matches D1. */
import { createRequire } from 'node:module'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

// `node:sqlite` is newer than the bundled Vite's builtin list, so a static
// `import` resolves to a bare `sqlite` package and fails; require it at runtime.
type DatabaseSync = { prepare(sql: string): SqliteStmt; exec(sql: string): void }
interface SqliteStmt {
  get(...args: never[]): Record<string, unknown> | undefined
  all(...args: never[]): Record<string, unknown>[]
  run(...args: never[]): { changes: number; lastInsertRowid: number | bigint }
}
const { DatabaseSync } = createRequire(import.meta.url)('node:sqlite') as {
  DatabaseSync: new (path: string) => DatabaseSync
}

const MIGRATIONS = join(__dirname, '..', '..', 'migrations')

interface Prepared {
  bind(...args: unknown[]): Prepared
  first<T = Record<string, unknown>>(col?: string): Promise<T | null>
  all<T = Record<string, unknown>>(): Promise<{ results: T[]; success: true; meta: object }>
  run(): Promise<{ success: true; meta: { last_row_id: number; changes: number } }>
}

function prepared(db: DatabaseSync, sql: string): Prepared {
  const stmt = db.prepare(sql)
  let args: unknown[] = []
  const self: Prepared = {
    bind(...a: unknown[]) {
      args = a
      return self
    },
    async first<T>(col?: string) {
      const row = stmt.get(...(args as never[])) as Record<string, unknown> | undefined
      if (row === undefined) return null
      return (col ? (row[col] as T) : (row as T)) ?? null
    },
    async all<T>() {
      return { results: stmt.all(...(args as never[])) as T[], success: true as const, meta: {} }
    },
    async run() {
      const r = stmt.run(...(args as never[]))
      return { success: true as const, meta: { last_row_id: Number(r.lastInsertRowid), changes: r.changes } }
    },
  }
  return self
}

/** A fresh in-memory D1 with every `migrations/*.sql` applied, in order. */
export function migratedD1(): D1Database {
  const db = new DatabaseSync(':memory:')
  for (const f of readdirSync(MIGRATIONS).filter(n => n.endsWith('.sql')).sort()) {
    db.exec(readFileSync(join(MIGRATIONS, f), 'utf8'))
  }
  const d1 = {
    prepare: (sql: string) => prepared(db, sql),
    async batch(stmts: Prepared[]) {
      const out = []
      for (const s of stmts) out.push(await s.run())
      return out
    },
    async exec(sql: string) {
      db.exec(sql)
      return { count: 0, duration: 0 }
    },
  }
  return d1 as unknown as D1Database
}
