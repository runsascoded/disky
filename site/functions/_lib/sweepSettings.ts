/** The sweep's deployment settings (D1 `sweep_settings`, migration `sweep-soft-delete`): one row, one typed column per setting,
 * so the database itself refuses a bad value (a key/value table couldn't CHECK this one, and it's a safety setting). Read at
 * dispatch (`sweepDispatch`), which passes it to the job on its command line: a running job can't change value mid-run. No row,
 * or a D1 without the table, reads as today's behaviour: 7 days. */
import type { D1Database } from '@cloudflare/workers-types'
import { DEFAULT_MIN_SOFT_DELETE_DAYS, MAX_MIN_SOFT_DELETE_DAYS } from '../../src/softDelete.js'

export const TABLE = 'sweep_settings'

export interface SweepSettings { min_soft_delete_days: number; updated_by: string; updated_at: number }

export const missingTable = (e: unknown): boolean => /no such table/.test(String((e as Error)?.message))

export async function getSweepSettings(db: D1Database): Promise<SweepSettings | null> {
  try {
    return await db.prepare(`SELECT min_soft_delete_days, updated_by, updated_at FROM ${TABLE} WHERE id = 1`).first<SweepSettings>()
  } catch (e) {
    if (missingTable(e)) return null
    throw e
  }
}

/** The minimum a run dispatched now runs under. */
export const minSoftDeleteDays = async (db: D1Database): Promise<number> =>
  (await getSweepSettings(db))?.min_soft_delete_days ?? DEFAULT_MIN_SOFT_DELETE_DAYS

/** The PUT body, or why it's refused. */
export function parseSweepSettings(b: unknown): { min_soft_delete_days: number } | string {
  const n = typeof b === 'object' && b !== null && !Array.isArray(b) ? (b as Record<string, unknown>).min_soft_delete_days : undefined
  if (typeof n !== 'number' || !Number.isInteger(n) || n < 0 || n > MAX_MIN_SOFT_DELETE_DAYS) return `min_soft_delete_days: an integer 0..${MAX_MIN_SOFT_DELETE_DAYS}`
  return { min_soft_delete_days: n }
}

/** Set the row and log its old and new value to `admin_edits`, in one transaction. */
export async function putSweepSettings(db: D1Database, s: { min_soft_delete_days: number }, who: string, ts: number): Promise<SweepSettings> {
  const old = await getSweepSettings(db)
  const row: SweepSettings = { ...s, updated_by: who, updated_at: ts }
  await db.batch([
    db.prepare(`INSERT INTO ${TABLE} (id, min_soft_delete_days, updated_by, updated_at) VALUES (1, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET min_soft_delete_days = excluded.min_soft_delete_days,
                  updated_by = excluded.updated_by, updated_at = excluded.updated_at`)
      .bind(row.min_soft_delete_days, row.updated_by, row.updated_at),
    db.prepare('INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES (?, ?, ?, ?, ?, ?, ?)')
      .bind(TABLE, '1', old ? 'update' : 'insert', who, ts, old ? JSON.stringify(old) : null, JSON.stringify(row)),
  ])
  return row
}
