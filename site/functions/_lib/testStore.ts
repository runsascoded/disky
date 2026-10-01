/**
 * Test-only: the index store over local fixture files, and the D1 rows that
 * point at them — so `openIndex` / `readRects` / `buildView` run end to end
 * over a real parquet generation in vitest. A test mocks the S3 store module
 * with `S3Store` from here:
 *
 *   vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))
 *
 * and registers each key the reader will ask for (`<dir>/path-index.parquet`,
 * its `.groups.json`, …) in `FILES`, or seeds a whole generation with
 * `seedGeneration`. Range reads slice the file; an unregistered key throws,
 * as a missing object would. Node-only modules are imported dynamically so
 * the Workers-typed `tsc -p functions` never resolves them; nothing outside
 * `*.test.ts` imports this file.
 */
import type { Sqlite } from './testD1.js'

/** Store key → local file. */
export const FILES = new Map<string, string>()
/** Every `get` the store served, in order — what a test asserts a read's I/O by. */
export const GETS: { key: string; offset?: number; length?: number }[] = []

/** What the real store throws for an absent key (`@rdub/file-tree`'s `NotFoundError`). */
class NotFoundError extends Error {
  constructor(key: string) {
    super(`no such key: ${key}`)
    this.name = 'NotFoundError'
  }
}

interface NodeFs {
  readFileSync(path: string): Uint8Array
  statSync(path: string): { size: number }
}
const load = async <T>(mod: string): Promise<T> => (await import(/* @vite-ignore */ mod)) as T

/** A `Store` (the parts the reader uses) over `FILES`. */
export function S3Store(_opts: unknown): { get(key: string, range?: { offset: number; length: number }): Promise<{ bytes: Uint8Array; totalSize: number }> } {
  return {
    async get(key, range) {
      const path = FILES.get(key)
      if (!path) throw new NotFoundError(key)
      GETS.push({ key, ...range })
      const fs = await load<NodeFs>('node:fs')
      const all = fs.readFileSync(path)
      const totalSize = all.byteLength
      const bytes = range ? all.slice(range.offset, range.offset + range.length) : all
      return { bytes, totalSize }
    },
  }
}

/** A fixture path, relative to this directory. */
export const fixture = (rel: string): string => new URL(`./fixtures/${rel}`, (import.meta as ImportMeta & { url: string }).url).pathname

/** A fixture file's size in bytes. */
export async function fixtureSize(rel: string): Promise<number> {
  const fs = await load<NodeFs>('node:fs')
  return fs.statSync(fixture(rel)).size
}

/** Parse a JSON fixture. */
export async function readJson<T>(rel: string): Promise<T> {
  const fs = await load<NodeFs>('node:fs')
  return JSON.parse(new TextDecoder().decode(fs.readFileSync(fixture(rel)))) as T
}

/** One variant's D1 rows as `index_footer.extract` produces them (the
 * `*.d1.json` fixtures): the schema pointer and every group's stats. */
export interface D1Variant {
  schema: { version: number; schema: unknown[]; floor_bytes?: number | null }
  rows: { rg: number; d_min: number; d_max: number; p_min: string; p_max: string; b_max: number; u_min: string | null; u_max: string | null; row_start: number; row_end: number; rg_json: string; b_min: number }[]
}

const q = (v: unknown): string => (v == null ? 'NULL' : `'${String(v).replace(/'/g, "''")}'`)

/** Publish a generation into a test D1 the way `index-sync` does: every
 * variant's group rows (unless `retired` names it — a retired tier
 * keeps its pointer and serves from its `.groups.parquet`, else the blob)
 * and the pointer row, as the primary store (no `store` column named) or a
 * secondary one (scoped rows, namespaced variant). Registers each variant's
 * parquet, `.groups.json` and (when given) `.groups.parquet` under `dir` in
 * `FILES`. */
export function seedGeneration(raw: Sqlite, o: {
  date: string
  gen: string
  dir: string
  variants: Record<string, D1Variant>
  /** Local fixture files per variant: `{ parquet, groups, footer? }` (relative
   * to `fixtures/`); `footer`, the `.groups.parquet`, is registered only when
   * given, so a retired variant without it is served from the blob. */
  files: Record<string, { parquet: string; groups: string; footer?: string }>
  store?: string
  /** Variants whose row groups are NOT written (blob-served). */
  retired?: string[]
}): void {
  const sec = o.store && o.store !== 'primary'
  const storeCol = sec ? 'store, ' : ''
  const storeVal = sec ? `${q(o.store)}, ` : ''
  for (const [variant, v] of Object.entries(o.variants)) {
    const d1v = sec ? `${o.store}:${variant}` : variant
    if (!(o.retired ?? []).includes(variant)) {
      for (const r of v.rows) {
        raw.exec(`INSERT INTO index_row_groups (${storeCol}date, variant, gen, rg, d_min, d_max, p_min, p_max, b_max, u_min, u_max, row_start, row_end, rg_json) VALUES (${storeVal}${q(o.date)}, ${q(d1v)}, ${q(o.gen)}, ${r.rg}, ${r.d_min}, ${r.d_max}, ${q(r.p_min)}, ${q(r.p_max)}, ${r.b_max}, ${q(r.u_min)}, ${q(r.u_max)}, ${r.row_start}, ${r.row_end}, ${q(r.rg_json)})`)
      }
    }
    raw.exec(`INSERT INTO index_schema (${storeCol}date, variant, version, schema_json, floor_bytes, gen, dir) VALUES (${storeVal}${q(o.date)}, ${q(d1v)}, ${v.schema.version}, ${q(JSON.stringify(v.schema.schema))}, ${v.schema.floor_bytes == null ? 'NULL' : v.schema.floor_bytes}, ${q(o.gen)}, ${q(o.dir)})`)
    const f = o.files[variant]
    // Where the reader looks for it — `index.ts` `indexKey`'s names, spelled
    // out: importing `./index` here would cycle through the mocked S3 module
    // (whose factory imports this file) and hang the test file at load.
    const tier = variant.endsWith('-user') ? variant.slice(0, -'-user'.length) : variant
    const sort = variant === 'user' || variant.endsWith('-user') ? '-by-user' : ''
    const key = `${o.dir}/path-index${tier === 'path' || tier === 'user' ? '' : `-${tier}`}${sort}.parquet`
    FILES.set(key, fixture(f.parquet))
    FILES.set(key.replace(/\.parquet$/, '.groups.json'), fixture(f.groups))
    if (f.footer) FILES.set(key.replace(/\.parquet$/, '.groups.parquet'), fixture(f.footer))
  }
}
