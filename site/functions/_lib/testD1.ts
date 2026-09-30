/**
 * Test-only: an in-memory D1 over `node:sqlite`, with a migrations lineage
 * (`site/migrations/<lineage>/*.sql`, in order) applied and foreign keys ON
 * (as D1 enforces them). Covers the D1 surface the Functions use:
 * `prepare(sql).bind(...).first/all/run`. Node-only modules are imported
 * dynamically so the Workers-typed `tsc -p functions` never resolves them;
 * nothing outside `*.test.ts` imports this file.
 */
import type { D1Database } from '@cloudflare/workers-types'

interface SqliteStmt {
  all(...params: unknown[]): Record<string, unknown>[]
  run(...params: unknown[]): { changes: number | bigint }
}
export interface Sqlite {
  exec(sql: string): void
  prepare(sql: string): SqliteStmt
}

const load = async <T>(mod: string): Promise<T> => (await import(/* @vite-ignore */ mod)) as T

interface NodeFs {
  readdirSync(dir: URL): string[]
  readFileSync(file: URL, encoding: 'utf8'): string
}

export const LINEAGES = ['cw', 'gcs'] as const

/** The lineage's migration files, sorted (`NNNN_name.sql`). */
export async function migrations(lineage: typeof LINEAGES[number]): Promise<{ name: string; sql: string }[]> {
  const { readdirSync, readFileSync } = await load<NodeFs>('node:fs')
  const dir = new URL(`../../migrations/${lineage}/`, (import.meta as ImportMeta & { url: string }).url)
  return readdirSync(dir).filter(f => f.endsWith('.sql')).sort()
    .map(name => ({ name, sql: readFileSync(new URL(name, dir), 'utf8') }))
}

function d1Of(raw: Sqlite): D1Database {
  const prepare = (sql: string, params: unknown[] = []) => ({
    bind: (...args: unknown[]) => prepare(sql, args),
    async first<T>(col?: string): Promise<T | null> {
      const row = raw.prepare(sql).all(...params)[0]
      if (!row) return null
      return (col ? row[col] : { ...row }) as T
    },
    async all<T>(): Promise<{ results: T[]; success: true; meta: Record<string, unknown> }> {
      return { results: raw.prepare(sql).all(...params).map(r => ({ ...r })) as T[], success: true, meta: {} }
    },
    async run(): Promise<{ success: true; meta: { changes: number } }> {
      const r = raw.prepare(sql).run(...params)
      return { success: true, meta: { changes: Number(r.changes) } }
    },
  })
  return { prepare: (sql: string) => prepare(sql) } as unknown as D1Database
}

/** A fresh in-memory DB with `lineage`'s migrations applied — all of them,
 * or (`before`) only those whose file name sorts before it, so a test can seed
 * rows and then apply a migration over them. */
export async function sqliteD1(lineage: typeof LINEAGES[number], { before }: { before?: string } = {}): Promise<{ db: D1Database; raw: Sqlite }> {
  const { DatabaseSync } = await load<{ DatabaseSync: new (path: string) => Sqlite }>('node:sqlite')
  const raw = new DatabaseSync(':memory:')
  raw.exec('PRAGMA foreign_keys = ON')
  for (const m of await migrations(lineage)) if (before === undefined || m.name < before) raw.exec(m.sql)
  return { db: d1Of(raw), raw }
}
