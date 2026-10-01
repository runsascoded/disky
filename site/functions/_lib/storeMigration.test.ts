import { describe, expect, it } from 'vitest'
import { migrations, sqliteD1 } from './testD1'

// The store-scoped index migration (specs/multi-store.md phase 1), in the `cw`
// D1 lineage (the only one `cloud` carries; gcs's lives on the `gcs` branch),
// applied over a seeded DB with foreign keys ON (as D1 enforces them): existing
// rows become the primary store's, nothing else moves, and the rebuilt pointer
// admits a second store's scan under the same id.
// `after`: what follows the store migration in its lineage today — a new
// migration is added here deliberately (and must not touch the index tables
// this file's specs pin).
const CASES = [
  { lineage: 'cw', file: '0006_store_scoped_index.sql', after: ['0007_agents.sql', '0008_freed_bytes.sql', '0009_allowlist_read_only.sql'] },
] as const

const SCHEMA_COLS = 'store, date, variant, version, schema_json, floor_bytes, gen, dir'

describe.each(CASES)('$lineage/$file', ({ lineage, file, after }) => {
  const seeded = async () => {
    const { raw } = await sqliteD1(lineage, { before: file })
    raw.exec(`
      INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann@example.test', 1);
      INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, 's3://b/tmp/', 'ann@example.test', 2);
      INSERT INTO index_schema (date, variant, version, schema_json, floor_bytes, gen, dir) VALUES
        ('2026-09-01', 'path', 1, '[]', NULL, 'g1', 'listing/2026-09-01/index/g1'),
        ('2026-09-01', 'coarse20', 1, '[]', 1048576, 'g1', 'listing/2026-09-01/index/g1');
      INSERT INTO index_row_groups (date, variant, gen, rg, d_min, d_max, p_min, p_max, b_max, u_min, u_max, row_start, row_end, rg_json) VALUES
        ('2026-09-01', 'path', 'g1', 0, 0, 1, 'a', 'b', 10, NULL, NULL, 0, 5, '[5,"ZSTD",[]]'),
        ('2026-09-01', 'path', 'g1', 1, 1, 2, 'b', 'c', 20, NULL, NULL, 5, 9, '[4,"ZSTD",[]]');
    `)
    const mig = (await migrations(lineage)).find(m => m.name === file)!
    raw.exec(mig.sql)
    return raw
  }

  it('is followed in its lineage by exactly the migrations listed', async () => {
    const names = (await migrations(lineage)).map(m => m.name)
    expect(names.slice(names.indexOf(file))).toEqual([file, ...after])
  })

  it('keeps every row, as the primary store, with foreign keys intact', async () => {
    const raw = await seeded()
    expect(raw.prepare(`SELECT ${SCHEMA_COLS} FROM index_schema ORDER BY variant`).all()).toEqual([
      { store: 'primary', date: '2026-09-01', variant: 'coarse20', version: 1, schema_json: '[]', floor_bytes: 1048576, gen: 'g1', dir: 'listing/2026-09-01/index/g1' },
      { store: 'primary', date: '2026-09-01', variant: 'path', version: 1, schema_json: '[]', floor_bytes: null, gen: 'g1', dir: 'listing/2026-09-01/index/g1' },
    ])
    expect(raw.prepare('SELECT store, date, variant, gen, rg, b_max FROM index_row_groups ORDER BY rg').all()).toEqual([
      { store: 'primary', date: '2026-09-01', variant: 'path', gen: 'g1', rg: 0, b_max: 10 },
      { store: 'primary', date: '2026-09-01', variant: 'path', gen: 'g1', rg: 1, b_max: 20 },
    ])
    expect(raw.prepare('SELECT id, name FROM plans').all()).toEqual([{ id: 1, name: 'Staged' }])
    expect(raw.prepare('SELECT plan_id, prefix FROM plan_items').all()).toEqual([{ plan_id: 1, prefix: 's3://b/tmp/' }])
    expect(raw.prepare('PRAGMA foreign_key_check').all()).toEqual([])
    expect(raw.prepare('PRAGMA integrity_check').all()).toEqual([{ integrity_check: 'ok' }])
  })

  it('puts `store` first in the pointer key; the row-group key is unchanged', async () => {
    const raw = await seeded()
    const pk = (t: string) => raw.prepare(`SELECT name FROM pragma_table_info('${t}') WHERE pk > 0 ORDER BY pk`).all().map(r => r.name)
    expect(pk('index_schema')).toEqual(['store', 'date', 'variant'])
    expect(pk('index_row_groups')).toEqual(['date', 'variant', 'gen', 'rg'])
    expect(raw.prepare("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'index_row_groups' AND sql IS NOT NULL ORDER BY name").all()).toEqual([
      { name: 'idx_index_row_groups_depth' },
      { name: 'idx_index_row_groups_user' },
    ])
  })

  it('admits a secondary store’s scan under a primary scan’s id, and still refuses a duplicate', async () => {
    const raw = await seeded()
    // As `index-sync --store meta` writes it: same scan id and gen, namespaced variant.
    raw.exec("INSERT INTO index_schema (store, date, variant, version, schema_json, gen, dir) VALUES ('meta', '2026-09-01', 'meta:path', 1, '[]', 'g1', 'meta-l2/2026-09-01/index/g1')")
    raw.exec("INSERT INTO index_row_groups (store, date, variant, gen, rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end, rg_json) VALUES ('meta', '2026-09-01', 'meta:path', 'g1', 0, 0, 1, 'a', 'b', 7, 0, 3, '[3,\"ZSTD\",[]]')")
    expect(raw.prepare("SELECT store, variant, gen FROM index_schema WHERE date = '2026-09-01' AND variant LIKE '%path' ORDER BY store").all()).toEqual([
      { store: 'meta', variant: 'meta:path', gen: 'g1' },
      { store: 'primary', variant: 'path', gen: 'g1' },
    ])
    let err: unknown
    try {
      raw.exec("INSERT INTO index_schema (date, variant, version, schema_json) VALUES ('2026-09-01', 'path', 1, '[]')")
    } catch (e) { err = e }
    expect(String((err as Error).message)).toBe('UNIQUE constraint failed: index_schema.store, index_schema.date, index_schema.variant')
  })
})
