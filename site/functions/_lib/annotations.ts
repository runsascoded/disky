/** Chart annotations in D1 (`chart_annotations`, migration `chart-annotations`; spec `chart-annotations.md`).
 *  Every write appends its old / new row to `admin_edits` (`tbl = 'chart_annotations'`, pk = the id). A D1
 *  without the table reads as no notes (`null` from `listNotes`), so a deployment keeps serving before its
 *  migration is applied. */
import type { D1Database } from '@cloudflare/workers-types'
import type { ChartAnnotation } from './annotationShape.js'

export const TABLE = 'chart_annotations'

export const missingTable = (e: unknown): boolean => /no such table/.test(String((e as Error)?.message))

const COLS = 'id, at, text, author, created, updated'

/** The store's notes, by `at` then id; null = the table doesn't exist. */
export async function listNotes(db: D1Database, store: string): Promise<ChartAnnotation[] | null> {
  try {
    return (await db.prepare(`SELECT ${COLS} FROM ${TABLE} WHERE store = ? ORDER BY at, id`).bind(store).all<ChartAnnotation>()).results
  } catch (e) {
    if (missingTable(e)) return null
    throw e
  }
}

const getNote = (db: D1Database, store: string, id: number) =>
  db.prepare(`SELECT ${COLS} FROM ${TABLE} WHERE store = ? AND id = ?`).bind(store, id).first<ChartAnnotation>()

const audit = (db: D1Database, pk: number, action: 'insert' | 'update' | 'delete', who: string, ts: number, old: ChartAnnotation | null, new_: ChartAnnotation | null) =>
  db.prepare('INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .bind(TABLE, String(pk), action, who, ts, old ? JSON.stringify(old) : null, new_ ? JSON.stringify(new_) : null)

export async function addNote(db: D1Database, store: string, note: { at: string; text: string }, who: string, ts: number): Promise<ChartAnnotation> {
  const row = await db.prepare(`INSERT INTO ${TABLE} (store, at, text, author, created, updated) VALUES (?, ?, ?, ?, ?, ?) RETURNING ${COLS}`)
    .bind(store, note.at, note.text, who, ts, ts).first<ChartAnnotation>()
  if (!row) throw new Error(`${TABLE}: insert returned no row`)
  await audit(db, row.id, 'insert', who, ts, null, row).run()
  return row
}

/** null = no such note in this store. The editor becomes its `author`. */
export async function editNote(db: D1Database, store: string, id: number, patch: { at?: string; text?: string }, who: string, ts: number): Promise<ChartAnnotation | null> {
  const old = await getNote(db, store, id)
  if (!old) return null
  const row: ChartAnnotation = { ...old, ...patch, author: who, updated: ts }
  await db.batch([
    db.prepare(`UPDATE ${TABLE} SET at = ?, text = ?, author = ?, updated = ? WHERE id = ?`).bind(row.at, row.text, who, ts, id),
    audit(db, id, 'update', who, ts, old, row),
  ])
  return row
}

/** null = no such note in this store. */
export async function deleteNote(db: D1Database, store: string, id: number, who: string, ts: number): Promise<ChartAnnotation | null> {
  const old = await getNote(db, store, id)
  if (!old) return null
  await db.batch([
    db.prepare(`DELETE FROM ${TABLE} WHERE id = ?`).bind(id),
    audit(db, id, 'delete', who, ts, old, null),
  ])
  return old
}
