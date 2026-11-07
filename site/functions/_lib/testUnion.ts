/**
 * Test-only: one deployment of three stores for the union-view tests (`_lib/union.ts`) — the primary
 * (`v2-search`: roots `bk`, `zz`) scanned at Z and A, then `v2` at C (`bk` alone, other content: a real diff); a
 * secondary `meta` (`v2-mount`: root `mb`) on its own schedule, A and B; a secondary `dup` (`v2`: a `bk` root,
 * overlapping the primary's) at A. A test mocks the S3 store module with `testStore`'s, as `testStore.ts` says.
 */
import type { Env } from './auth.js'
import { sqliteD1 } from './testD1.js'
import { type D1Variant, readJson, seedGeneration } from './testStore.js'

export const Z = '2026-09-30T0001'
export const A = '2026-10-01T0001'
export const B = '2026-10-02T0001'
export const C = '2026-10-03T0001'

const CREDS = { STORE_ACCESS_KEY_ID: 'STORE_META_ID', STORE_SECRET_ACCESS_KEY: 'STORE_META_ID' }
const META = { vars: { ROOT_LABEL: 'our storage', SNAPSHOTS_SUBDIR: 'meta', STORE_PREFIXES: 'meta-l2/,snapshots/meta/', STORE_BUCKET: 'meta-data' }, secrets: CREDS }
const DUP = { vars: { ROOT_LABEL: 'dup', STORE_BUCKET: 'dup-data' }, secrets: CREDS }

/** The main union view the tests default to. */
export const MAIN = { all: { union: ['primary', 'meta'], main: true } }

/** The seeded deployment's env, given its union views (none = a deployment without one). */
export async function unionDeployment(): Promise<(views?: Record<string, unknown>) => Env> {
  const { db, raw } = await sqliteD1()
  const seed = async (fx: string, date: string, store?: string) => {
    const v = await readJson<Record<string, D1Variant>>(`${fx}/d1.json`)
    const files = { path: { parquet: `${fx}/path-index.parquet`, groups: `${fx}/path-index.groups.json` }, bysize: { parquet: `${fx}/path-index-bysize.parquet`, groups: `${fx}/path-index-bysize.groups.json` } }
    seedGeneration(raw, { date, gen: 'g', dir: `${store ?? 'primary'}/${date}/index/g`, variants: v, files, store })
  }
  for (const d of [Z, A]) await seed('v2-search', d)
  await seed('v2', C)
  for (const d of [A, B]) await seed('v2-mount', d, 'meta')
  await seed('v2', A, 'dup')
  const base = { DB: db, GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data', ROOT_LABEL: 'Fleet', STORE_META_ID: 'x' } as Env
  return (views = MAIN) => ({ ...base, STORES_JSON: JSON.stringify({ meta: META, dup: DUP, ...views }) })
}
