import { describe, expect, it } from 'vitest'
import { LINEAGES, migrationNamed, MIGRATIONS, migrations, schemaFork, sqliteD1 } from './testD1'

interface NodeFs {
  existsSync(file: URL): boolean
  readFileSync(file: URL, encoding: 'utf8'): string
}
const NODE_FS = 'node:fs'
const { existsSync, readFileSync } = (await import(/* @vite-ignore */ NODE_FS)) as NodeFs

/** A migration's SQL without its comments or whitespace runs: what a twin must match. */
const normalized = (sql: string) => sql.replace(/--[^\n]*/g, '').replace(/\s+/g, ' ').trim()

/** `site/migrations/twins.json`: groups of `<lineage>/<file>` that are the
 * same migration under each lineage's numbering (`scripts/port-migration`). */
const TWINS = JSON.parse(readFileSync(new URL('twins.json', MIGRATIONS), 'utf8')) as string[][]

describe('the lineages are the directories under `site/migrations/`', () => {
  it('the base lineage `cw` is one; sorted; `twins.json` is not one', () => {
    expect([LINEAGES.filter(l => l === 'cw' || l.includes('.')), [...LINEAGES].sort()]).toEqual([['cw'], [...LINEAGES]])
  })
})

describe.each(LINEAGES)('the %s lineage', lineage => {
  it('starts at `0001_init.sql`', async () => {
    expect((await migrations(lineage))[0].name).toBe('0001_init.sql')
  })

  it('migrationNamed finds a migration by its name, whatever its number', async () => {
    const names = (await migrations(lineage)).map(m => m.name)
    const named = await migrationNamed(lineage, 'store_scoped_index')
    expect(names.filter(n => /^\d+_store_scoped_index\.sql$/.test(n))).toEqual([named])
    await expect(migrationNamed(lineage, 'no_such_migration')).rejects.toThrow(`${lineage}: 0 migrations named \`no_such_migration\` ()`)
  })

  it('its schema fork: the `deletion_runs` column holding the run’s manifest dir', async () => {
    const { raw } = await sqliteD1(lineage)
    const cols = raw.prepare("SELECT name FROM pragma_table_info('deletion_runs')").all().map(r => r.name)
    expect(cols.filter(c => c === schemaFork(lineage).runDir)).toEqual([schemaFork(lineage).runDir])
  })
})

describe('migration twins (`site/migrations/twins.json`)', () => {
  it('each group spans distinct lineages, at least two', () => {
    expect(TWINS.filter(g => g.length < 2 || new Set(g.map(t => t.split('/')[0])).size !== g.length)).toEqual([])
  })

  it('every twin in a lineage this branch carries exists', () => {
    const present = TWINS.flat().filter(t => LINEAGES.includes(t.split('/')[0]))
    expect(present.filter(t => !existsSync(new URL(t, MIGRATIONS)))).toEqual([])
  })

  it('twins present together are the same SQL, modulo comments and whitespace', () => {
    const sql = (t: string) => normalized(readFileSync(new URL(t, MIGRATIONS), 'utf8'))
    const groups = TWINS.map(g => g.filter(t => LINEAGES.includes(t.split('/')[0]))).filter(g => g.length > 1)
    expect(groups.flatMap(g => g.slice(1).filter(t => sql(t) !== sql(g[0])).map(t => [g[0], t]))).toEqual([])
  })
})
