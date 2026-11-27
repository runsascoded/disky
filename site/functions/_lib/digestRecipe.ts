/** The `digest-thread-variant` D1 recipe (specs/digest-thread-variant.md: `digest_threads`, `digest_settings.variant`,
 *  `digest_reapply.variant`), which each deployment applies on its own go. Until it has, the digest routes read its
 *  columns as NULL and its table as no row, and refuse a write that needs them (`recipeMissing`, 503). */
import type { D1Database } from '@cloudflare/workers-types'
import { json } from './auth.js'

export const RECIPE = 'digest-thread-variant'

/** Whether `db` has the recipe (one probe of its last column). */
export async function hasThreadVariant(db: D1Database): Promise<boolean> {
  try {
    await db.prepare('SELECT variant FROM digest_reapply LIMIT 0').all()
    return true
  } catch (e) {
    if (/no such (column|table)/.test(String((e as Error)?.message))) return false
    throw e
  }
}

export const RECIPE_MISSING = `not configured: apply the D1 recipe ${RECIPE} (site/migrations/${RECIPE}.sql)`
export const recipeMissing = (): Response => json({ error: RECIPE_MISSING }, 503, { 'cache-control': 'no-store' })
