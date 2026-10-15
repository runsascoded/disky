/**
 * Test-only: an in-memory D1 over `node:sqlite`, holding the branch's D1
 * schema (`site/schema.sql`, specs/done/d1-migrations.md) with foreign keys ON
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

/** `site/schema.sql`: the branch's D1 schema, its current state. */
export const SCHEMA = new URL('../../schema.sql', (import.meta as ImportMeta & { url: string }).url)
const SCHEMA_SQL = (await load<{ readFileSync(file: URL, encoding: 'utf8'): string }>('node:fs')).readFileSync(SCHEMA, 'utf8')

async function rawDb(): Promise<Sqlite> {
  const { DatabaseSync } = await load<{ DatabaseSync: new (path: string) => Sqlite }>('node:sqlite')
  const raw = new DatabaseSync(':memory:')
  raw.exec('PRAGMA foreign_keys = ON')
  raw.exec(SCHEMA_SQL)
  return raw
}

/** The `deletion_runs` column holding a run's manifest dir, as this branch's
 * schema names it (`manifest` in cloud's; gcs's squashed init calls it `plan`),
 * so the sweep tests run on either. */
export const RUN_DIR: string = await (async () => {
  const cols = (await rawDb()).prepare("SELECT name FROM pragma_table_info('deletion_runs')").all().map(r => r.name)
  const hits = ['manifest', 'plan'].filter(c => cols.includes(c))
  if (hits.length !== 1) throw new Error(`deletion_runs: want one of manifest | plan, got [${cols.join(', ')}]`)
  return hits[0]
})()

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

/** A fresh in-memory D1 holding the schema. */
export async function sqliteD1(): Promise<{ db: D1Database; raw: Sqlite }> {
  const raw = await rawDb()
  return { db: d1Of(raw), raw }
}

/** The ownership ledger's tables (specs/file-assign.md), exactly as gcs's schema has them. */
const LEDGER_TABLES = ['actions', 'owner_prefixes', 'owner_totals', 'claims'] as const
const LEDGER_DDL = `
  CREATE TABLE actions (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, ts INTEGER NOT NULL, scan TEXT NOT NULL, pattern TEXT NOT NULL, set_owner INTEGER NOT NULL DEFAULT 0, owner TEXT, set_keep INTEGER NOT NULL DEFAULT 0, keep TEXT, memo TEXT, kind TEXT CHECK (kind IS NULL OR kind = 'object'), CHECK (set_owner OR set_keep));
  CREATE TABLE owner_prefixes (action_id INTEGER NOT NULL REFERENCES actions (id), prefix TEXT NOT NULL, owner TEXT, ts INTEGER NOT NULL, tombstoned TEXT, kind TEXT CHECK (kind IS NULL OR kind = 'object'), PRIMARY KEY (prefix, action_id));
  CREATE TABLE owner_totals (scan TEXT NOT NULL, head INTEGER NOT NULL, body TEXT NOT NULL, claims TEXT NOT NULL, computed_ts INTEGER NOT NULL, ms INTEGER, PRIMARY KEY (scan, head));
  CREATE TABLE claims (prefix TEXT PRIMARY KEY, who TEXT NOT NULL, ts INTEGER NOT NULL);
`

const tables = (raw: Sqlite): string[] => raw.prepare("SELECT name FROM sqlite_master WHERE type = 'table'").all().map(r => String(r.name))

/** The ownership ledger, for tests of the code that reads or writes it: gcs's
 * schema has it (nothing to do); `cloud`'s has none, so it's created here as
 * gcs's schema has it. */
export function withLedger(raw: Sqlite): void {
  const have = tables(raw).filter(t => (LEDGER_TABLES as readonly string[]).includes(t))
  if (have.length === LEDGER_TABLES.length) return
  if (have.length) throw new Error(`a partial ledger: [${have.join(', ')}]`)
  raw.exec(LEDGER_DDL)
}

/** No ownership ledger, for tests of a deployment without one (cw's schema),
 * whichever branch's schema the database was built from. */
export function withoutLedger(raw: Sqlite): void {
  for (const t of ['owner_prefixes', 'owner_totals', 'claims', 'actions']) raw.exec(`DROP TABLE IF EXISTS ${t}`)
}
