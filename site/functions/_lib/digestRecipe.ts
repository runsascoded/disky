/** The digest thread recipes (specs/digest-thread-variant.md), which each deployment applies on its own go:
 *  `digest-thread-variant` (`digest_threads`, `digest_settings.variant`, `digest_reapply.variant`) and after it
 *  `digest-thread-switch` (`digest_threads.edits`, `.reorder`). Until a D1 has one, the digest routes read its columns
 *  as NULL and its table as no row, and refuse a write that needs them (`recipeMissing`, 503). */
import type { D1Database } from '@cloudflare/workers-types'
import { json } from './auth.js'

export const RECIPE = 'digest-thread-variant'
export const SWITCH_RECIPE = 'digest-thread-switch'

async function probe(db: D1Database, sql: string): Promise<boolean> {
  try {
    await db.prepare(sql).all()
    return true
  } catch (e) {
    if (/no such (column|table)/.test(String((e as Error)?.message))) return false
    throw e
  }
}

/** Whether `db` has `digest-thread-variant` (one probe of its last column). */
export const hasThreadVariant = (db: D1Database): Promise<boolean> => probe(db, 'SELECT variant FROM digest_reapply LIMIT 0')
/** Whether `db` has `digest-thread-switch`. */
export const hasThreadSwitch = (db: D1Database): Promise<boolean> => probe(db, 'SELECT reorder FROM digest_threads LIMIT 0')

export const missingRecipe = (name: string): string => `not configured: apply the D1 recipe ${name} (site/migrations/${name}.sql)`
export const RECIPE_MISSING = missingRecipe(RECIPE)
export const recipeMissing = (name: string = RECIPE): Response => json({ error: missingRecipe(name) }, 503, { 'cache-control': 'no-store' })
