import { describe, expect, it } from 'vitest'
import { RUN_DIR, SCHEMA, type Sqlite, sqliteD1, withLedger, withoutLedger } from './testD1'

interface NodeFs { readFileSync(file: URL, encoding: 'utf8'): string }
const NODE_FS = 'node:fs'
const { readFileSync } = (await import(/* @vite-ignore */ NODE_FS)) as NodeFs

/** The tables `site/schema.sql` creates, in file order. */
const TABLES = [...readFileSync(SCHEMA, 'utf8').matchAll(/^CREATE TABLE "?(\w+)"?/gm)].map(m => m[1])

describe('sqliteD1: the branch’s schema (`site/schema.sql`)', () => {
  it('holds every table of the schema file, foreign keys ON, nothing else', async () => {
    const { raw } = await sqliteD1()
    const tables = raw.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").all().map(r => r.name)
    expect([tables, raw.prepare('PRAGMA foreign_keys').all()]).toEqual([[...TABLES].sort(), [{ foreign_keys: 1 }]])
  })

  it('`RUN_DIR` is the `deletion_runs` column holding a run’s manifest dir', async () => {
    const { raw } = await sqliteD1()
    const cols = raw.prepare("SELECT name FROM pragma_table_info('deletion_runs')").all().map(r => r.name)
    expect(cols.filter(c => c === 'manifest' || c === 'plan')).toEqual([RUN_DIR])
  })

  /** The ledger tables' shape: columns (type, not null, default, pk), foreign keys, check text. */
  const ledgerShape = (raw: Sqlite) => ['actions', 'owner_prefixes', 'owner_totals', 'claims'].map(t => ({
    t,
    cols: raw.prepare(`SELECT name, type, "notnull", dflt_value, pk FROM pragma_table_info('${t}')`).all().map(r => ({ ...r })),
    fks: raw.prepare(`SELECT "table", "from", "to" FROM pragma_foreign_key_list('${t}')`).all().map(r => ({ ...r })),
    checks: [...String(raw.prepare(`SELECT sql FROM sqlite_master WHERE name = '${t}'`).all()[0].sql).matchAll(/CHECK \((.*?)\)(?=,|\n|\)$)/g)].map(m => m[1]),
  }))

  it.skipIf(!TABLES.includes('actions'))('`withLedger` builds the ledger exactly as the schema has it (where it does)', async () => {
    const [{ raw: schema }, { raw: built }] = await Promise.all([sqliteD1(), sqliteD1()])
    withoutLedger(built)
    withLedger(built)
    expect(ledgerShape(built)).toEqual(ledgerShape(schema))
  })

  it('`withoutLedger` leaves no ledger table, `withLedger` all four', async () => {
    const { raw } = await sqliteD1()
    const ledger = () => raw.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('actions', 'owner_prefixes', 'owner_totals', 'claims') ORDER BY name").all().map(r => r.name)
    withoutLedger(raw)
    const none = ledger()
    withLedger(raw)
    expect([none, ledger()]).toEqual([[], ['actions', 'claims', 'owner_prefixes', 'owner_totals']])
  })
})
