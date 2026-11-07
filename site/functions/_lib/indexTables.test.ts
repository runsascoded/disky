import { describe, expect, it } from 'vitest'
import { sqliteD1 } from './testD1'

// The index tables' keys as the store-scoped index (specs/done/multi-store.md
// phase 1) left them, in the branch's schema: `store` leads the pointer key, the
// row-group key is unchanged and store-free, and a secondary store's scan sits
// beside a primary scan of the same id. A recipe that rebuilds or re-keys these
// tables has to keep every assertion here.

const SCHEMA_COLS = 'store, date, variant, version, schema_json, floor_bytes, gen, dir'

describe('the index tables (`index_schema`, `index_row_groups`)', () => {
  it('puts `store` first in the pointer key; the row-group key is store-free', async () => {
    const { raw } = await sqliteD1()
    const pk = (t: string) => raw.prepare(`SELECT name FROM pragma_table_info('${t}') WHERE pk > 0 ORDER BY pk`).all().map(r => r.name)
    expect(pk('index_schema')).toEqual(['store', 'date', 'variant'])
    expect(pk('index_row_groups')).toEqual(['date', 'variant', 'gen', 'rg'])
    expect(raw.prepare("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'index_row_groups' AND sql IS NOT NULL ORDER BY name").all()).toEqual([
      { name: 'idx_index_row_groups_depth' },
      { name: 'idx_index_row_groups_user' },
    ])
  })

  it('a pointer defaults to the primary store and an unknown start (`started` NULL), which a sync can then record', async () => {
    const { raw } = await sqliteD1()
    raw.exec("INSERT INTO index_schema (date, variant, version, schema_json, gen, dir) VALUES ('2026-09-01', 'path', 2, '[]', 'g1', 'listing/2026-09-01/index/g1')")
    expect(raw.prepare(`SELECT ${SCHEMA_COLS}, started FROM index_schema`).all()).toEqual([
      { store: 'primary', date: '2026-09-01', variant: 'path', version: 2, schema_json: '[]', floor_bytes: null, gen: 'g1', dir: 'listing/2026-09-01/index/g1', started: null },
    ])
    raw.exec("UPDATE index_schema SET started = '2026-09-01T10:00:01.000Z' WHERE store = 'primary' AND date = '2026-09-01' AND variant = 'path' AND started IS NULL")
    expect(raw.prepare('SELECT store, started FROM index_schema').all()).toEqual([{ store: 'primary', started: '2026-09-01T10:00:01.000Z' }])
  })

  it('admits a secondary store’s scan under a primary scan’s id, and still refuses a duplicate', async () => {
    const { raw } = await sqliteD1()
    raw.exec(`
      INSERT INTO index_schema (date, variant, version, schema_json, gen, dir) VALUES ('2026-09-01', 'path', 1, '[]', 'g1', 'listing/2026-09-01/index/g1');
      INSERT INTO index_row_groups (date, variant, gen, rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end, rg_json) VALUES
        ('2026-09-01', 'path', 'g1', 0, 0, 1, 'a', 'b', 10, 0, 5, '[5,"ZSTD",[]]');
    `)
    // As `index-sync --store meta` writes it: same scan id and gen, namespaced variant.
    raw.exec("INSERT INTO index_schema (store, date, variant, version, schema_json, gen, dir) VALUES ('meta', '2026-09-01', 'meta:path', 1, '[]', 'g1', 'meta-l2/2026-09-01/index/g1')")
    raw.exec("INSERT INTO index_row_groups (store, date, variant, gen, rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end, rg_json) VALUES ('meta', '2026-09-01', 'meta:path', 'g1', 0, 0, 1, 'a', 'b', 7, 0, 3, '[3,\"ZSTD\",[]]')")
    expect(raw.prepare("SELECT store, variant, gen FROM index_schema WHERE date = '2026-09-01' AND variant LIKE '%path' ORDER BY store").all()).toEqual([
      { store: 'meta', variant: 'meta:path', gen: 'g1' },
      { store: 'primary', variant: 'path', gen: 'g1' },
    ])
    expect(raw.prepare('SELECT store, variant, b_max FROM index_row_groups ORDER BY store').all()).toEqual([
      { store: 'meta', variant: 'meta:path', b_max: 7 },
      { store: 'primary', variant: 'path', b_max: 10 },
    ])
    let err: unknown
    try {
      raw.exec("INSERT INTO index_schema (date, variant, version, schema_json) VALUES ('2026-09-01', 'path', 1, '[]')")
    } catch (e) { err = e }
    expect(String((err as Error).message)).toBe('UNIQUE constraint failed: index_schema.store, index_schema.date, index_schema.variant')
  })
})
