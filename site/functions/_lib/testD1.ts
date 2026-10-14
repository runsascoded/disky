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
  readdirSync(dir: URL, opts: { withFileTypes: true }): { name: string; isDirectory(): boolean }[]
  readFileSync(file: URL, encoding: 'utf8'): string
}

/** `site/migrations/`: one directory per D1 lineage, plus `twins.json`. */
export const MIGRATIONS = new URL('../../migrations/', (import.meta as ImportMeta & { url: string }).url)

/** A D1 migrations lineage: a directory under `site/migrations/`. */
export type Lineage = string

/** The lineages this branch carries, sorted: every directory under
 * `site/migrations/` (the base carries `cw`; a deployment branch may add its
 * own, and every lineage-agnostic D1 test then runs on it too). */
export const LINEAGES: readonly Lineage[] = (await load<NodeFs>('node:fs'))
  .readdirSync(MIGRATIONS, { withFileTypes: true }).filter(e => e.isDirectory()).map(e => e.name).sort()

/** The lineage's migration files, sorted (`NNNN_name.sql`). */
export async function migrations(lineage: Lineage): Promise<{ name: string; sql: string }[]> {
  const { readdirSync, readFileSync } = await load<NodeFs>('node:fs')
  const dir = new URL(`${lineage}/`, MIGRATIONS)
  return readdirSync(dir).filter(f => f.endsWith('.sql')).sort()
    .map(name => ({ name, sql: readFileSync(new URL(name, dir), 'utf8') }))
}

/** The one migration of `lineage` named `NNNN_<stem>.sql`, whatever its
 * number there (lineages number the same migration differently). */
export async function migrationNamed(lineage: Lineage, stem: string): Promise<string> {
  const hits = (await migrations(lineage)).map(m => m.name).filter(n => n.replace(/^\d+_/, '') === `${stem}.sql`)
  if (hits.length !== 1) throw new Error(`${lineage}: ${hits.length} migrations named \`${stem}\` (${hits.join(', ')})`)
  return hits[0]
}

/** Where the lineages' schemas really differ, as the shared tests need it,
 * per lineage — the column holding a deletion run's manifest dir
 * (`deletion_runs.manifest` on cw, `.plan` on gcs's squashed init). A
 * lineage absent here has cw's shape; `testD1.test.ts` pins each entry
 * against the schema of every lineage this branch carries. */
export interface SchemaFork { runDir: string }
const SCHEMA_FORKS: Record<Lineage, SchemaFork> = {
  cw: { runDir: 'manifest' },
  gcs: { runDir: 'plan' },
}
export const schemaFork = (lineage: Lineage): SchemaFork => SCHEMA_FORKS[lineage] ?? SCHEMA_FORKS.cw

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
    /** For `batch`: a read returns its rows, a write its changes. */
    async step(): Promise<{ results: unknown[]; success: true; meta: Record<string, unknown> }> {
      if (/^\s*(SELECT|WITH)\b/i.test(sql)) return this.all()
      const r = raw.prepare(sql).run(...params)
      return { results: [], success: true, meta: { changes: Number(r.changes) } }
    },
  })
  // `batch`: D1 runs the statements in order, as one transaction.
  const batch = async (stmts: { step(): Promise<unknown> }[]) => {
    raw.exec('BEGIN')
    try {
      const out = []
      for (const st of stmts) out.push(await st.step())
      raw.exec('COMMIT')
      return out
    } catch (e) {
      raw.exec('ROLLBACK')
      throw e
    }
  }
  return { prepare: (sql: string) => prepare(sql), batch } as unknown as D1Database
}

/** A fresh in-memory DB with `lineage`'s migrations applied — all of them,
 * or (`before`) only those whose file name sorts before it, so a test can seed
 * rows and then apply a migration over them. */
export async function sqliteD1(lineage: Lineage, { before }: { before?: string } = {}): Promise<{ db: D1Database; raw: Sqlite }> {
  const { DatabaseSync } = await load<{ DatabaseSync: new (path: string) => Sqlite }>('node:sqlite')
  const raw = new DatabaseSync(':memory:')
  raw.exec('PRAGMA foreign_keys = ON')
  for (const m of await migrations(lineage)) if (before === undefined || m.name < before) raw.exec(m.sql)
  return { db: d1Of(raw), raw }
}

/** The gcs lineage's `kind` migration, ledger half (specs/file-assign.md): what
 * `migrations/gcs/` adds beside cw's `0017_plan_items_kind.sql` (whose
 * `plan_items` statement it repeats). `cloud` carries no gcs migrations, so
 * tests that build the gcs ledger tables ad hoc append this to them. */
export const LEDGER_KIND = `
  ALTER TABLE actions ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
  ALTER TABLE owner_prefixes ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
`

/** The gcs ledger tables as `migrations/gcs/0001_init.sql` has them (keep
 * columns dropped by 0028), with `LEDGER_KIND` applied. */
export const GCS_LEDGER = `
  CREATE TABLE actions (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, ts INTEGER NOT NULL, scan TEXT NOT NULL, pattern TEXT NOT NULL, set_owner INTEGER NOT NULL DEFAULT 0, owner TEXT, memo TEXT);
  CREATE TABLE owner_prefixes (action_id INTEGER NOT NULL REFERENCES actions (id), prefix TEXT NOT NULL, owner TEXT, ts INTEGER NOT NULL, tombstoned TEXT, PRIMARY KEY (prefix, action_id));
  ${LEDGER_KIND}
`
