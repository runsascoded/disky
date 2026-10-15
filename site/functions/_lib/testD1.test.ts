import { describe, expect, it } from 'vitest'
import { RUN_DIR, SCHEMA, sqliteD1 } from './testD1'

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
})
